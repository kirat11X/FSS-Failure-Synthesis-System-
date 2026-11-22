package Ingestion;

import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicReference;

import Storage.BlockAllocator;
import Storage.BlockMetadataStore;
import Storage.BlockStore;
import Storage.SegmentManager;
import Storage.SegmentMetadataStore;
import Storage.StorageConfig;
import Storage.StorageWorker;
import Storage.TelemetryBuffer;

public final class IngestionStorageRuntime implements AutoCloseable {

    private final TelemetryBuffer buffer;
    private final TelemetryIngestor ingestor;

    private final BlockStore blockStore;
    private final BlockMetadataStore metadataStore;
    private final SegmentMetadataStore segmentStore;

    private final Thread storageThread;
    private final AtomicReference<Throwable> workerFailure = new AtomicReference<>();

    private IngestionStorageRuntime(StorageConfig config) throws Exception {

        blockStore = new BlockStore(
                config.root.resolve("blocks.dat"));

        metadataStore = new BlockMetadataStore(
                config.root.resolve("block-metadata.dat"),
                config.blockSize);

        segmentStore = new SegmentMetadataStore(config.root);

        BlockAllocator allocator = new BlockAllocator(
                config.blockSize,
                metadataStore);

        SegmentManager segmentManager = new SegmentManager(
                config,
                blockStore,
                metadataStore,
                allocator,
                segmentStore);

        buffer = new TelemetryBuffer(config.bufferCapacityBytes);

        StorageWorker worker = new StorageWorker(
                config,
                buffer,
                segmentManager);

        storageThread = Thread.ofPlatform()
                .name("storage-worker")
                .start(() -> {
                    try {
                        worker.processUntilClosed();
                    } catch (Throwable e) {
                        workerFailure.set(e);
                        buffer.close();
                    }
                });

        ingestor = new TelemetryIngestor(
                buffer,
                config.envelope,
                config.maxMessageSize);
    }

    public static IngestionStorageRuntime start(Path propertiesPath)
            throws Exception {

        StorageConfig config = StorageConfig.load(propertiesPath);

        java.nio.file.Files.createDirectories(config.root);

        return new IngestionStorageRuntime(config);
    }

    public TelemetryIngestor ingestor() {
        return ingestor;
    }

    @Override
    public void close() throws Exception {

        buffer.close();
        storageThread.join();

        try {
            if (workerFailure.get() != null) {
                throw new RuntimeException(
                        "Storage worker failed",
                        workerFailure.get());
            }
        } finally {
            try {
                metadataStore.close();
            } finally {

                blockStore.close();
            }
        }
    }
}
