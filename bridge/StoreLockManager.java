package bridge;

import fss.core.BlockStore;

import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantReadWriteLock;

/**
 * Concurrency and file-locking manager for 64KB Java BlockStore files.
 *
 * Enforces:
 *  1. In-JVM ReentrantReadWriteLock per store path (prevents OverlappingFileLockException).
 *  2. Cross-process OS record locking via FileChannel.lock() on a sidecar .lock file.
 *  3. Transient lock contention retry loop.
 *  4. Explicit metadata and data flushing via force(true).
 */
public final class StoreLockManager {

    private static final ConcurrentHashMap<String, ReentrantReadWriteLock> JVM_LOCKS = new ConcurrentHashMap<>();

    private StoreLockManager() {}

    public static ReentrantReadWriteLock getJvmLock(Path storePath) {
        String key = storePath.toAbsolutePath().normalize().toString();
        return JVM_LOCKS.computeIfAbsent(key, k -> new ReentrantReadWriteLock(true)); // Fair locking
    }

    /**
     * Acquire exclusive write lock: first in-JVM write lock, then sidecar file lock.
     */
    public static AutoCloseable acquireWriteLock(Path storePath) throws IOException {
        ReentrantReadWriteLock rwLock = getJvmLock(storePath);
        rwLock.writeLock().lock();

        Path norm = storePath.toAbsolutePath().normalize();
        if (norm.getParent() != null) {
            Files.createDirectories(norm.getParent());
        }
        Path lockFilePath = Path.of(norm.toString() + ".lock");

        FileChannel lockChannel = null;
        FileLock fileLock = null;
        try {
            lockChannel = FileChannel.open(
                    lockFilePath,
                    StandardOpenOption.CREATE,
                    StandardOpenOption.READ,
                    StandardOpenOption.WRITE
            );
            fileLock = lockChannel.lock(); // Blocks until exclusive OS lock acquired
        } catch (Throwable t) {
            if (lockChannel != null) {
                try {
                    lockChannel.close();
                } catch (Exception ignored) {}
            }
            rwLock.writeLock().unlock();
            if (t instanceof IOException ioe) {
                throw ioe;
            }
            throw new IOException("Failed to acquire OS file lock on " + lockFilePath, t);
        }

        final FileChannel finalChannel = lockChannel;
        final FileLock finalLock = fileLock;

        return () -> {
            try {
                if (finalLock != null && finalLock.isValid()) {
                    finalLock.release();
                }
            } finally {
                try {
                    if (finalChannel != null && finalChannel.isOpen()) {
                        finalChannel.force(true);
                        finalChannel.close();
                    }
                } finally {
                    rwLock.writeLock().unlock();
                }
            }
        };
    }

    /**
     * Acquire read lock across processes and threads.
     * Since BlockStore.open requires exclusive channel lock, access is serialized cleanly.
     */
    public static AutoCloseable acquireReadLock(Path storePath) throws IOException {
        return acquireWriteLock(storePath);
    }

    /**
     * Open BlockStore with contention retry to protect against transient OS file-close latency.
     */
    public static BlockStore openWithRetry(Path storePath, int timeoutMs) throws IOException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (true) {
            try {
                return BlockStore.open(storePath);
            } catch (IOException ioe) {
                if (ioe.getMessage() != null && ioe.getMessage().contains("locked by another process")
                        && System.currentTimeMillis() < deadline) {
                    try {
                        Thread.sleep(20);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        throw ioe;
                    }
                    continue;
                }
                throw ioe;
            }
        }
    }
}
