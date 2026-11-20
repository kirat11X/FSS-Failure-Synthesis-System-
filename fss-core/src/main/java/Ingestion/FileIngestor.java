package Ingestion;

import java.io.BufferedInputStream;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import Storage.BlockAllocator;
import Storage.BlockMetadataStore;
import Storage.BlockStore;
import Storage.SegmentManager;
import Storage.SegmentMetadataStore;
import Storage.StorageConfig;
import Storage.StorageWorker;
import Storage.TelemetryBuffer;

public final class FileIngestor {

    public static int ingestFile(Path propertiesPath, Path binFilePath, boolean cleanFirst) throws Exception {
        StorageConfig config = StorageConfig.load(propertiesPath);

        if (cleanFirst && Files.exists(config.root)) {
            try (var paths = Files.walk(config.root)) {
                paths.sorted(java.util.Comparator.reverseOrder())
                     .forEach(p -> {
                         try { Files.delete(p); } catch (Exception ignored) {}
                     });
            }
        }
        Files.createDirectories(config.root);

        BlockStore blockStore = new BlockStore(config.root.resolve("blocks.dat"));
        BlockMetadataStore metadataStore = new BlockMetadataStore(
                config.root.resolve("block-metadata.dat"), config.blockSize);
        SegmentMetadataStore segmentStore = new SegmentMetadataStore(config.root);
        BlockAllocator allocator = new BlockAllocator(config.blockSize, metadataStore);
        SegmentManager segmentManager = new SegmentManager(
                config, blockStore, metadataStore, allocator, segmentStore);

        TelemetryBuffer buffer = new TelemetryBuffer(config.bufferCapacityBytes);
        StorageWorker worker = new StorageWorker(config, buffer, segmentManager);

        AtomicReference<Throwable> workerFailure = new AtomicReference<>();
        Thread workerThread = new Thread(() -> {
            try {
                worker.processUntilClosed();
            } catch (Throwable e) {
                workerFailure.set(e);
            }
        }, "file-ingestor-worker");

        workerThread.start();
        TelemetryIngestor ingestor = new TelemetryIngestor(buffer, config.envelope, config.maxMessageSize);
        AtomicInteger count = new AtomicInteger(0);

        try (InputStream rawIn = new BufferedInputStream(Files.newInputStream(binFilePath))) {
            byte[] header = new byte[20];

            while (true) {
                int read = 0;
                while (read < 20) {
                    int r = rawIn.read(header, read, 20 - read);
                    if (r < 0) break;
                    read += r;
                }
                if (read == 0) break;
                if (read < 20) break;

                ByteBuffer bb = ByteBuffer.wrap(header).order(config.envelope.byteOrder());
                int payloadLen = bb.getInt(12);

                if (payloadLen < 0 || (20 + payloadLen) > config.maxMessageSize) {
                    break;
                }

                byte[] payload = new byte[payloadLen];
                int pRead = 0;
                while (pRead < payloadLen) {
                    int r = rawIn.read(payload, pRead, payloadLen - pRead);
                    if (r < 0) break;
                    pRead += r;
                }
                if (pRead < payloadLen) break;

                byte[] fullMessage = new byte[20 + payloadLen];
                System.arraycopy(header, 0, fullMessage, 0, 20);
                System.arraycopy(payload, 0, fullMessage, 20, payloadLen);

                IngestionResult result = ingestor.ingest(fullMessage);
                if (result == IngestionResult.ACCEPTED) {
                    count.incrementAndGet();
                }
            }
        } finally {
            buffer.close();
            workerThread.join();
            metadataStore.close();
            blockStore.close();
        }

        if (workerFailure.get() != null) {
            throw new RuntimeException("Storage worker failed during ingestion", workerFailure.get());
        }

        return count.get();
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: FileIngestor <storage.properties> <file.bin> [--append]");
            System.exit(1);
        }
        Path prop = Path.of(args[0]);
        Path bin = Path.of(args[1]);
        boolean clean = args.length < 3 || !args[2].equals("--append");
        int ingested = ingestFile(prop, bin, clean);
        System.out.println("Ingested " + ingested + " events from " + bin);
    }
}
