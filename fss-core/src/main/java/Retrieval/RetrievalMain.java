package Retrieval;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.nio.file.Files;
import java.nio.file.Path;

import Storage.BlockMetadataStore;
import Storage.BlockStore;
import Storage.SegmentMetadataStore;
import Storage.StorageConfig;

public final class RetrievalMain {

        public static void main(String[] args) throws Exception {
                // Capture raw stdout file descriptor for 4-byte Big-Endian IPC stream and redirect System.out to stderr immediately
                FileOutputStream rawOut = new FileOutputStream(FileDescriptor.out);
                System.setOut(System.err);

                Path propertiesPath = Path.of(
                                args.length > 0 ? args[0] : "storage.properties");

                StorageConfig config = StorageConfig.load(propertiesPath);

                Files.createDirectories(config.root);

                BlockStore blockStore = new BlockStore(
                                config.root.resolve("blocks.dat"));

                BlockMetadataStore metadataStore = new BlockMetadataStore(
                                config.root.resolve("block-metadata.dat"),
                                config.blockSize);

                SegmentMetadataStore segmentStore = new SegmentMetadataStore(config.root);

                try {
                        RetrievalService retrievalService = new RetrievalService(
                                        config,
                                        blockStore,
                                        metadataStore,
                                        segmentStore);

                        RetrievalIpcServer server = new RetrievalIpcServer(retrievalService, System.in, rawOut);

                        System.err.println("Retrieval IPC server ready");
                        server.run();

                } finally {
                        metadataStore.close();
                        blockStore.close();
                        rawOut.close();
                }
        }
}
