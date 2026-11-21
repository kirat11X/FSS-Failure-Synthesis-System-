package Storage;

import fss.core.EnvelopeCodec;
import fss.core.EventRecord;

import java.io.EOFException;
import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantReadWriteLock;
import java.util.stream.Stream;

/**
 * Non-invasive segment compactor and retention manager (REQ-019).
 *
 * Guarantees:
 *  - Operates strictly on sealed segments/blocks (never touches active blocks being written).
 *  - Validates CRC-32C checksums of all records/blocks during compaction.
 *  - Merges sparse sealed blocks into compact blocks without violating 64KB block boundaries or CRC-32C framing.
 *  - Atomically updates store files and cleans up unlinked / superseded blocks.
 */
public final class DynamicSegmentCompactor {

    private static final int BLOCK_SIZE = EnvelopeCodec.BLOCK_SIZE; // 65,536 bytes
    private static final ConcurrentHashMap<String, ReentrantReadWriteLock> LOCKS = new ConcurrentHashMap<>();

    public record CompactionResult(
            int initialBlocks,
            int compactedBlocks,
            long bytesReclaimed,
            int recordsPreserved,
            int segmentsRecycled
    ) {
        public boolean isSuccess() {
            return true;
        }

        public long reclaimedBytes() {
            return bytesReclaimed;
        }

        public String toJson() {
            return String.format(
                    "{\"status\":\"OK\",\"initial_blocks\":%d,\"compacted_blocks\":%d,\"bytes_reclaimed\":%d,\"records_preserved\":%d,\"segments_recycled\":%d}",
                    initialBlocks, compactedBlocks, bytesReclaimed, recordsPreserved, segmentsRecycled
            );
        }
    }

    public DynamicSegmentCompactor() {
    }

    private static ReentrantReadWriteLock getLock(Path path) {
        String key = path.toAbsolutePath().normalize().toString();
        return LOCKS.computeIfAbsent(key, k -> new ReentrantReadWriteLock(true));
    }

    /**
     * Compacts sealed segments in the store at {@code storePath}, purging records
     * strictly prior to {@code retentionCutoffTimestamp} (unsigned comparison).
     */
    public CompactionResult compactSealedSegments(Path storePath, long retentionCutoffTimestamp) throws IOException {
        Objects.requireNonNull(storePath, "storePath cannot be null");
        Path normPath = storePath.toAbsolutePath().normalize();

        if (Files.isDirectory(normPath)) {
            return compactDirectorySegments(normPath, retentionCutoffTimestamp);
        }

        return compactBlockStoreFile(normPath, retentionCutoffTimestamp);
    }

    public CompactionResult compactSealedSegments(Path storePath) throws IOException {
        return compactSealedSegments(storePath, 0L);
    }

    /**
     * Purges expired segments/records strictly prior to {@code retentionCutoffTimestamp}.
     * Returns the count of segments/blocks recycled.
     */
    public int recycleExpiredSegments(Path storePath, long retentionCutoffTimestamp) throws IOException {
        CompactionResult res = compactSealedSegments(storePath, retentionCutoffTimestamp);
        return res.segmentsRecycled();
    }

    public int recycleExpiredSegments(Path storePath) throws IOException {
        return recycleExpiredSegments(storePath, 0L);
    }

    private record ParsedBlock(
            int blockId,
            int sourceId,
            boolean empty,
            int usedBytes,
            List<EventRecord> records,
            byte[] rawBlockData
    ) {}

    /**
     * Compacts a 64KB BlockStore (.dat) file.
     */
    private CompactionResult compactBlockStoreFile(Path filePath, long retentionCutoffTimestamp) throws IOException {
        if (!Files.exists(filePath)) {
            throw new IOException("Store file does not exist: " + filePath);
        }

        ReentrantReadWriteLock rwLock = getLock(filePath);
        rwLock.writeLock().lock();
        try {
            Path lockFilePath = Path.of(filePath.toString() + ".lock");
            try (FileChannel lockChan = FileChannel.open(
                    lockFilePath,
                    StandardOpenOption.CREATE,
                    StandardOpenOption.READ,
                    StandardOpenOption.WRITE);
                 FileLock osLock = lockChan.lock()) {

                return doCompactFile(filePath, retentionCutoffTimestamp);
            }
        } finally {
            rwLock.writeLock().unlock();
        }
    }

    private CompactionResult doCompactFile(Path filePath, long retentionCutoffTimestamp) throws IOException {
        long originalFileSize = Files.size(filePath);
        if (originalFileSize == 0) {
            return new CompactionResult(0, 0, 0L, 0, 0);
        }
        if (originalFileSize % BLOCK_SIZE != 0) {
            throw new IOException("Store file size " + originalFileSize + " is not a multiple of 64KB");
        }

        int totalBlocks = (int) (originalFileSize / BLOCK_SIZE);
        List<ParsedBlock> parsedBlocks = new ArrayList<>(totalBlocks);
        Map<Integer, Integer> latestBlockBySource = new HashMap<>();

        try (FileChannel channel = FileChannel.open(filePath, StandardOpenOption.READ)) {
            ByteBuffer buf = ByteBuffer.allocate(BLOCK_SIZE).order(ByteOrder.LITTLE_ENDIAN);

            for (int blockId = 0; blockId < totalBlocks; blockId++) {
                buf.clear();
                channel.position((long) blockId * BLOCK_SIZE);
                while (buf.hasRemaining()) {
                    int r = channel.read(buf);
                    if (r < 0) {
                        throw new EOFException("Premature EOF at block " + blockId);
                    }
                }
                buf.flip();

                byte[] raw = new byte[BLOCK_SIZE];
                buf.get(raw);
                buf.rewind();

                // Scan block
                int off = 0;
                boolean empty = true;
                int sourceId = -1;
                List<EventRecord> blockRecords = new ArrayList<>();

                while (BLOCK_SIZE - off >= EnvelopeCodec.MIN_RECORD_SIZE) {
                    int recordLen = buf.getInt(off);
                    if (recordLen == 0) {
                        break;
                    }
                    empty = false;
                    EnvelopeCodec.Error err = EnvelopeCodec.checkRecord(buf, off, BLOCK_SIZE - off);
                    if (err != null) {
                        throw new IOException("CRC-32C validation failed on block " + blockId + " at offset " + off + ": " + err);
                    }

                    int envelopeOff = off + EnvelopeCodec.RECORD_HEADER_LEN;
                    EventRecord ev = EnvelopeCodec.decodeEnvelope(buf, envelopeOff, recordLen);
                    if (sourceId == -1) {
                        sourceId = ev.sourceId();
                    }
                    blockRecords.add(ev);
                    off += EnvelopeCodec.RECORD_HEADER_LEN + recordLen;
                }

                ParsedBlock pb = new ParsedBlock(blockId, sourceId, empty, off, blockRecords, raw);
                parsedBlocks.add(pb);
                if (!empty && sourceId != -1) {
                    latestBlockBySource.put(sourceId, blockId);
                }
            }
        }

        // Identify active vs sealed blocks
        Set<Integer> activeBlockIds = new HashSet<>(latestBlockBySource.values());

        // For each source, collect valid unexpired records from its sealed blocks
        Map<Integer, List<EventRecord>> retainedSealedBySource = new HashMap<>();
        int sealedBlocksCount = 0;
        int sealedRecordsCount = 0;

        for (ParsedBlock pb : parsedBlocks) {
            if (activeBlockIds.contains(pb.blockId())) {
                continue; // Active block, do not touch
            }
            if (pb.empty()) {
                sealedBlocksCount++;
                continue;
            }
            sealedBlocksCount++;
            sealedRecordsCount += pb.records().size();

            List<EventRecord> list = retainedSealedBySource.computeIfAbsent(pb.sourceId(), s -> new ArrayList<>());
            for (EventRecord ev : pb.records()) {
                if (retentionCutoffTimestamp > 0 && Long.compareUnsigned(ev.timestampNs(), retentionCutoffTimestamp) < 0) {
                    // Expired record, purge
                    continue;
                }
                list.add(ev);
            }
        }

        // Build newly compacted sealed blocks
        List<byte[]> newSealedBlocks = new ArrayList<>();
        int recordsPreserved = 0;

        List<Integer> sources = new ArrayList<>(retainedSealedBySource.keySet());
        sources.sort(Integer::compareUnsigned);

        for (int srcId : sources) {
            List<EventRecord> records = retainedSealedBySource.get(srcId);
            records.sort(Comparator.comparingLong(EventRecord::timestampNs));

            ByteBuffer blockBuf = ByteBuffer.allocate(BLOCK_SIZE).order(ByteOrder.LITTLE_ENDIAN);
            for (EventRecord ev : records) {
                byte[] encoded = EnvelopeCodec.encodeRecord(ev);
                if (blockBuf.remaining() < encoded.length) {
                    // Flush current full sealed block
                    newSealedBlocks.add(blockBuf.array());
                    blockBuf = ByteBuffer.allocate(BLOCK_SIZE).order(ByteOrder.LITTLE_ENDIAN);
                }
                blockBuf.put(encoded);
                recordsPreserved++;
            }
            if (blockBuf.position() > 0) {
                newSealedBlocks.add(blockBuf.array());
            }
        }

        // Collect active blocks (untouched in byte form)
        List<byte[]> activeBlocks = new ArrayList<>();
        for (ParsedBlock pb : parsedBlocks) {
            if (activeBlockIds.contains(pb.blockId())) {
                activeBlocks.add(pb.rawBlockData());
                recordsPreserved += pb.records().size();
            }
        }

        // Total new blocks
        int newTotalBlocks = newSealedBlocks.size() + activeBlocks.size();
        int initialBlocks = totalBlocks;
        long bytesReclaimed = Math.max(0L, (long) (initialBlocks - newTotalBlocks) * BLOCK_SIZE);
        int segmentsRecycled = Math.max(0, sealedBlocksCount - newSealedBlocks.size());

        // Atomically write new compacted file
        Path parent = filePath.getParent();
        if (parent == null) {
            parent = Path.of(".");
        }
        Path tempFile = Files.createTempFile(parent, filePath.getFileName().toString() + ".", ".compact.tmp");

        try {
            try (FileChannel outChan = FileChannel.open(
                    tempFile,
                    StandardOpenOption.CREATE,
                    StandardOpenOption.WRITE,
                    StandardOpenOption.TRUNCATE_EXISTING)) {

                for (byte[] blk : newSealedBlocks) {
                    ByteBuffer b = ByteBuffer.wrap(blk);
                    while (b.hasRemaining()) {
                        outChan.write(b);
                    }
                }
                for (byte[] blk : activeBlocks) {
                    ByteBuffer b = ByteBuffer.wrap(blk);
                    while (b.hasRemaining()) {
                        outChan.write(b);
                    }
                }
                outChan.force(true);
            }

            Files.move(tempFile, filePath, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } finally {
            Files.deleteIfExists(tempFile);
        }

        return new CompactionResult(
                initialBlocks,
                newTotalBlocks,
                bytesReclaimed,
                recordsPreserved,
                segmentsRecycled
        );
    }

    /**
     * Compacts segments in a Storage root directory.
     */
    private CompactionResult compactDirectorySegments(Path dirPath, long retentionCutoffTimestamp) throws IOException {
        Path segmentsDir = dirPath.resolve("segments");
        if (!Files.exists(segmentsDir)) {
            return new CompactionResult(0, 0, 0L, 0, 0);
        }

        int segmentsRecycled = 0;
        long bytesReclaimed = 0;

        try (Stream<Path> stream = Files.list(segmentsDir)) {
            List<Path> segmentDirs = stream.filter(Files::isDirectory).toList();
            for (Path sDir : segmentDirs) {
                String name = sDir.getFileName().toString();
                if (name.startsWith("segment-")) {
                    try {
                        long segStart = Long.parseLong(name.substring("segment-".length()));
                        if (retentionCutoffTimestamp > 0 && segStart < retentionCutoffTimestamp) {
                            try (Stream<Path> files = Files.walk(sDir)) {
                                for (Path p : files.sorted(Comparator.reverseOrder()).toList()) {
                                    if (Files.isRegularFile(p)) {
                                        bytesReclaimed += Files.size(p);
                                    }
                                    Files.deleteIfExists(p);
                                }
                            }
                            segmentsRecycled++;
                        }
                    } catch (NumberFormatException ignored) {}
                }
            }
        }

        return new CompactionResult(
                segmentsRecycled,
                0,
                bytesReclaimed,
                0,
                segmentsRecycled
        );
    }

    public static void main(String[] args) {
        if (args.length == 0) {
            System.err.println("Usage: java Storage.DynamicSegmentCompactor <store_path> [retention_cutoff_ns]");
            System.exit(1);
        }

        String storePathStr = null;
        long cutoff = 0L;

        for (int i = 0; i < args.length; i++) {
            if ("--store".equals(args[i]) && i + 1 < args.length) {
                storePathStr = args[++i];
            } else if ("--cutoff".equals(args[i]) && i + 1 < args.length) {
                cutoff = Long.parseUnsignedLong(args[++i]);
            } else if ("compact".equalsIgnoreCase(args[i]) || "recycle".equalsIgnoreCase(args[i])) {
                // Command verb
            } else if (!args[i].startsWith("-")) {
                if (storePathStr == null) {
                    storePathStr = args[i];
                } else {
                    try {
                        cutoff = Long.parseUnsignedLong(args[i]);
                    } catch (NumberFormatException ignored) {}
                }
            }
        }

        if (storePathStr == null) {
            System.err.println("Error: Store path required");
            System.exit(1);
        }

        try {
            DynamicSegmentCompactor compactor = new DynamicSegmentCompactor();
            CompactionResult res = compactor.compactSealedSegments(Path.of(storePathStr), cutoff);
            System.out.println(res.toJson());
            System.exit(0);
        } catch (Exception e) {
            System.err.println("Compaction failed: " + e.getMessage());
            e.printStackTrace(System.err);
            System.exit(2);
        }
    }
}