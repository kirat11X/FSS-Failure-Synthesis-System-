package Ingestion;

import java.io.BufferedInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;

import Storage.StorageConfig;

/**
 * CLI ingestor that streams wire-format binary telemetry files into the configured
 * Java BlockStore via {@link IngestionStorageRuntime}, ensuring clean buffer flush
 * and shutdown before returning.
 */
public final class FileIngestor {

    private FileIngestor() {
    }

    public static int ingestFile(Path propertiesPath, Path binFilePath, boolean cleanFirst) throws Exception {
        StorageConfig config = StorageConfig.load(propertiesPath);

        if (cleanFirst && Files.exists(config.root)) {
            try (var paths = Files.walk(config.root)) {
                paths.sorted(Comparator.reverseOrder())
                        .forEach(p -> {
                            try {
                                Files.delete(p);
                            } catch (IOException e) {
                                throw new RuntimeException("Failed to clean storage path: " + p, e);
                            }
                        });
            }
        }

        int headerLen = config.envelope.field("Payload").start();
        int payloadLenOffset = config.envelope.field("Payload Length").start();
        int acceptedCount = 0;

        try (IngestionStorageRuntime runtime = IngestionStorageRuntime.start(propertiesPath);
             InputStream rawIn = new BufferedInputStream(Files.newInputStream(binFilePath))) {

            TelemetryIngestor ingestor = runtime.ingestor();
            byte[] header = new byte[headerLen];

            while (true) {
                int headerRead = readFullyOrEof(rawIn, header, headerLen);
                if (headerRead == 0) {
                    break;
                }
                if (headerRead < headerLen) {
                    throw new EOFException("Truncated envelope header at end of " + binFilePath);
                }

                long payloadLenLong = Integer.toUnsignedLong(
                        ByteBuffer.wrap(header)
                                .order(config.envelope.byteOrder())
                                .getInt(payloadLenOffset));

                if (payloadLenLong > Integer.MAX_VALUE || (headerLen + payloadLenLong) > config.maxMessageSize) {
                    throw new IOException(
                            "Invalid envelope payload length " + payloadLenLong
                                    + " (maxMessageSize=" + config.maxMessageSize + ")");
                }

                int payloadLen = (int) payloadLenLong;
                byte[] fullMessage = new byte[headerLen + payloadLen];
                System.arraycopy(header, 0, fullMessage, 0, headerLen);

                int payloadRead = readFullyOrEof(rawIn, fullMessage, headerLen, payloadLen);
                if (payloadRead < payloadLen) {
                    throw new EOFException("Truncated envelope payload at end of " + binFilePath);
                }

                IngestionResult result = ingestor.ingest(fullMessage);
                if (result == IngestionResult.ACCEPTED) {
                    acceptedCount++;
                } else {
                    throw new IOException("Telemetry frame rejected by ingestor: " + result);
                }
            }
        }

        return acceptedCount;
    }

    private static int readFullyOrEof(InputStream in, byte[] buf, int len) throws IOException {
        return readFullyOrEof(in, buf, 0, len);
    }

    private static int readFullyOrEof(InputStream in, byte[] buf, int offset, int len) throws IOException {
        int total = 0;
        while (total < len) {
            int r = in.read(buf, offset + total, len - total);
            if (r < 0) {
                break;
            }
            total += r;
        }
        return total;
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: FileIngestor <storage.properties> <file.bin> [--append]");
            System.exit(1);
        }
        Path prop = Path.of(args[0]);
        Path bin = Path.of(args[1]);
        boolean clean = args.length < 3 || !"--append".equals(args[2]);
        int ingested = ingestFile(prop, bin, clean);
        System.out.println("Ingested " + ingested + " events from " + bin);
    }
}
