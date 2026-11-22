package Ingestion;

import java.nio.file.Path;

import Storage.StorageConfig;

public final class SyntheticDataMain {

    public static void main(String[] args) throws Exception {

        Path propertiesPath = Path.of("storage.properties");

        StorageConfig config = StorageConfig.load(propertiesPath);

        try (IngestionStorageRuntime runtime = IngestionStorageRuntime.start(propertiesPath)) {

            SyntheticTelemetryProducer producer = new SyntheticTelemetryProducer(
                    runtime.ingestor(),
                    config.envelope);

            Thread producerThread = Thread.ofPlatform()
                    .name("synthetic-telemetry-producer")
                    .start(producer);

            System.out.println("[Main] Synthetic ingestion runtime started.");
            System.out.println("[Main] Press Ctrl+C to stop.");

            try {
                producerThread.join();
            } finally {
                producer.stop();
                producerThread.interrupt();
                producerThread.join();
            }
        }
    }

    private SyntheticDataMain() {
    }
}
