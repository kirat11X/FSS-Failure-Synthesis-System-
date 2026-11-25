package Storage;

import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;

public final class StorageConfig {
    public final Path root;
    public final int blockSize;
    public final int bufferCapacityBytes;
    public final int maxMessageSize;
    public final long segmentDurationMillis;
    public final EnvelopeConfig envelope;

    private StorageConfig(
            Path root, int blockSize, int bufferCapacityBytes,
            int maxMessageSize, long segmentDurationMillis,
            EnvelopeConfig envelope) {
        this.root = root;
        this.blockSize = blockSize;
        this.bufferCapacityBytes = bufferCapacityBytes;
        this.maxMessageSize = maxMessageSize;
        this.segmentDurationMillis = segmentDurationMillis;
        this.envelope = envelope;
    }

    public static StorageConfig load(Path propertiesPath) throws IOException {
        Properties p = new Properties();
        try (InputStream in = Files.newInputStream(propertiesPath)) {
            p.load(in);
        }

        Path base = propertiesPath.toAbsolutePath().getParent();
        String rawRoot = required(p, "root").trim();
        Path root = resolve(base, rawRoot);
        boolean isDefaultRepoRoot = "./test-data".equals(rawRoot) || "test-data".equals(rawRoot);
        boolean allowFallback = Boolean.parseBoolean(p.getProperty("fallbackToRepoDefaults", "true"));
        if (isDefaultRepoRoot && allowFallback && !hasData(root)) {
            Path alt1 = base.resolve("test_data").resolve("test-data");
            Path alt2 = base.resolve("test-data");
            Path alt3 = base.resolve("test_data");
            if (hasData(alt1)) {
                root = alt1;
            } else if (hasData(alt2)) {
                root = alt2;
            } else if (hasData(alt3)) {
                root = alt3;
            }
        }
        Path envelopePath = resolve(base, required(p, "envelopeConfig"));
        if (!Files.exists(envelopePath)) {
            Path altEnv = base.resolve("fss-core").resolve("envelope.cfg");
            if (Files.exists(altEnv)) {
                envelopePath = altEnv;
            }
        }

        int blockSize = integer(p, "blockSize");
        int bufferCapacity = integer(p, "bufferCapacityBytes");
        int maxMessage = integer(p, "maxMessageSize");
        long segmentDuration = longValue(p, "segmentDurationMillis");

        ByteOrder order = switch (p.getProperty("byteOrder", "BIG_ENDIAN").trim().toUpperCase()) {
            case "BIG_ENDIAN" -> ByteOrder.BIG_ENDIAN;
            case "LITTLE_ENDIAN" -> ByteOrder.LITTLE_ENDIAN;
            default -> throw new IOException("Invalid byteOrder");
        };
        if (maxMessage + Long.BYTES > blockSize) {
            throw new IOException(
                    "maxMessageSize must not exceed blockSize");
        }

        EnvelopeConfig envelope = EnvelopeConfig.load(envelopePath, order);
        envelope.validateAgainstMaxMessageSize(maxMessage);
        if (blockSize <= 0 || maxMessage <= 0 || segmentDuration <= 0) {
            throw new IOException("Block size, message size and segment duration must be positive");
        }
        if (bufferCapacity < maxMessage) {
            throw new IOException("bufferCapacityBytes must be >= maxMessageSize");
        }

        return new StorageConfig(root, blockSize, bufferCapacity,
                maxMessage, segmentDuration, envelope);
    }

    private static Path resolve(Path base, String value) {
        Path path = Path.of(value);
        return path.isAbsolute() ? path : base.resolve(path).normalize();
    }

    private static boolean hasData(Path dir) {
        if (dir == null || !Files.isDirectory(dir)) {
            return false;
        }
        Path blocks = dir.resolve("blocks.dat");
        try {
            return Files.isRegularFile(blocks) && Files.size(blocks) > 0;
        } catch (IOException e) {
            return false;
        }
    }

    private static String required(Properties p, String key) throws IOException {
        String value = p.getProperty(key);
        if (value == null || value.isBlank()) {
            throw new IOException("Missing property: " + key);
        }
        return value.trim();
    }

    private static int integer(Properties p, String key) throws IOException {
        try {
            return Integer.parseInt(required(p, key));
        } catch (NumberFormatException e) {
            throw new IOException("Invalid integer property: " + key, e);
        }
    }

    private static long longValue(Properties p, String key) throws IOException {
        try {
            return Long.parseLong(required(p, key));
        } catch (NumberFormatException e) {
            throw new IOException("Invalid long property: " + key, e);
        }
    }
}
