package bridge;

import fss.core.BlockInfo;
import fss.core.BlockStore;
import fss.core.EnvelopeCodec;
import fss.core.EventRecord;
import fss.core.RetrieveRequest;
import fss.core.RetrieveResult;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Non-invasive Java-to-Python bridge CLI.
 *
 * Provides:
 *   - ingest: Ingests wire envelope stream fixtures into a durable BlockStore.
 *   - retrieve: Performs bounded retrieval and streams envelopes to stdout with JSON metadata header.
 *   - synthesize / pipeline: End-to-end trigger that retrieves events and spawns the Python pipeline.
 */
public final class JavaStoreBridge {

    public static void main(String[] args) {
        if (args.length == 0 || args[0].equals("-h") || args[0].equals("--help") || args[0].equals("help")) {
            printHelp();
            return;
        }

        String command = args[0].toLowerCase();
        try {
            switch (command) {
                case "ingest" -> runIngest(args);
                case "retrieve" -> runRetrieve(args);
                case "synthesize", "pipeline" -> runPipeline(args);
                default -> {
                    System.err.println("Unknown command: " + command);
                    printHelp();
                    System.exit(1);
                }
            }
        } catch (Exception ex) {
            System.err.println("Error executing " + command + ": " + ex.getMessage());
            ex.printStackTrace(System.err);
            System.exit(2);
        }
    }

    private static void printHelp() {
        System.out.println("JavaStoreBridge: FSS Java Core & Python Pipeline Linkage");
        System.out.println("Usage: java -cp <classpath> bridge.JavaStoreBridge <command> [options]");
        System.out.println();
        System.out.println("Commands:");
        System.out.println("  ingest      Ingest wire envelope (.bin) fixtures into a BlockStore data file");
        System.out.println("              Options: --store <path> [--fixture <bin_path>...]");
        System.out.println();
        System.out.println("  retrieve    Retrieve bounded events from BlockStore, streaming envelopes to stdout");
        System.out.println("              Options: --store <path> [--source <ALL|id>] [--start <ns|ISO>]");
        System.out.println("                       [--end <ns|ISO>] [--event-type <id>] [--max-events <n>]");
        System.out.println();
        System.out.println("  pipeline    Execute end-to-end retrieval, evidence decoding & failure synthesis");
        System.out.println("  synthesize  Options: [--store <path>] [--fixture <bin_path>] [--query <text>]");
        System.out.println("                       [--source <ALL|id>] [--start <ns|ISO>] [--end <ns|ISO>]");
        System.out.println("                       [--max-events <n>] [--python <bin>] [--schema <yaml>]");
    }

    private static void runIngest(String[] args) throws IOException {
        String storePath = null;
        List<String> fixtures = new ArrayList<>();

        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--store" -> storePath = args[++i];
                case "--fixture" -> fixtures.add(args[++i]);
                default -> {
                    if (storePath == null && !args[i].startsWith("-")) {
                        storePath = args[i];
                    } else if (!args[i].startsWith("-")) {
                        fixtures.add(args[i]);
                    }
                }
            }
        }

        if (storePath == null) {
            System.err.println("Error: --store <path> is required for ingest");
            System.exit(1);
        }
        if (fixtures.isEmpty()) {
            System.err.println("Error: At least one --fixture <bin_file> is required for ingest");
            System.exit(1);
        }

        Path sPath = Path.of(storePath);
        int totalIngested = 0;
        try (BlockStore store = BlockStore.open(sPath)) {
            for (String f : fixtures) {
                Path fPath = Path.of(f);
                if (!Files.exists(fPath)) {
                    System.err.println("Warning: fixture file not found: " + f);
                    continue;
                }
                byte[] data = Files.readAllBytes(fPath);
                int count = ingestEnvelopeStream(store, data);
                System.err.printf("Ingested %d events from %s%n", count, f);
                totalIngested += count;
            }
            store.sync();
        }
        System.out.printf("Successfully ingested %d events into BlockStore at %s%n", totalIngested, sPath);
    }

    private static int ingestEnvelopeStream(BlockStore store, byte[] data) throws IOException {
        ByteBuffer buf = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
        int off = 0;
        int count = 0;
        while (off < data.length) {
            if (data.length - off < EnvelopeCodec.ENVELOPE_HEADER_LEN) {
                break;
            }
            int payloadLen = buf.getInt(off + 20);
            int recordLen = EnvelopeCodec.ENVELOPE_HEADER_LEN + payloadLen;
            if (off + recordLen > data.length) {
                throw new IOException("Truncated envelope in stream at offset " + off);
            }
            EventRecord record = EnvelopeCodec.decodeEnvelope(buf, off, recordLen);
            store.append(record);
            count++;
            off += recordLen;
        }
        return count;
    }

    private static void runRetrieve(String[] args) throws IOException {
        String storePath = null;
        String sourceStr = "ALL";
        String startStr = null;
        String endStr = null;
        Integer eventType = null;
        int maxEvents = 100;

        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--store" -> storePath = args[++i];
                case "--source" -> sourceStr = args[++i];
                case "--start" -> startStr = args[++i];
                case "--end" -> endStr = args[++i];
                case "--event-type" -> eventType = parseEventType(args[++i]);
                case "--max-events" -> maxEvents = Integer.parseInt(args[++i]);
                default -> {
                    if (storePath == null && !args[i].startsWith("-")) {
                        storePath = args[i];
                    }
                }
            }
        }

        if (storePath == null) {
            System.err.println("Error: --store <path> is required for retrieve");
            System.exit(1);
        }

        Path sPath = Path.of(storePath);
        if (!Files.exists(sPath)) {
            System.err.println("Error: Store file not found: " + sPath);
            System.exit(1);
        }

        try (BlockStore store = BlockStore.open(sPath)) {
            long tStart = (startStr != null) ? parseTimeNs(startStr) : findMinTs(store);
            long tEnd = (endStr != null) ? parseTimeNs(endStr) : findMaxTs(store, tStart);

            RetrieveRequest req = buildRequest(sourceStr, tStart, tEnd, maxEvents, eventType);
            RetrieveResult res = store.retrieve(req);

            // Output JSON metadata header on the very first line of stdout
            String header = String.format("{\"truncated\": %b, \"count\": %d}\n", res.truncated(), res.events().size());
            System.out.write(header.getBytes(StandardCharsets.UTF_8));

            // Stream concatenated wire envelopes
            for (EventRecord ev : res.events()) {
                byte[] envelope = EnvelopeCodec.encodeEnvelope(ev);
                System.out.write(envelope);
            }
            System.out.flush();
        }
    }

    private static void runPipeline(String[] args) throws Exception {
        String storePath = null;
        String fixturePath = null;
        String query = "Why did drone 474 leave formation?";
        String sourceStr = "ALL";
        String startStr = null;
        String endStr = null;
        Integer eventType = null;
        int maxEvents = 100;
        String pythonBin = null;
        String schemaPath = "schemas/drone_v0.yaml";

        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--store" -> storePath = args[++i];
                case "--fixture" -> fixturePath = args[++i];
                case "--query" -> query = args[++i];
                case "--source" -> sourceStr = args[++i];
                case "--start" -> startStr = args[++i];
                case "--end" -> endStr = args[++i];
                case "--event-type" -> eventType = parseEventType(args[++i]);
                case "--max-events" -> maxEvents = Integer.parseInt(args[++i]);
                case "--python" -> pythonBin = args[++i];
                case "--schema" -> schemaPath = args[++i];
            }
        }

        // Auto-provision store if fixture provided
        Path sPath;
        if (storePath != null) {
            sPath = Path.of(storePath);
        } else {
            if (fixturePath == null) {
                fixturePath = "fixtures/golden/scenario_298_474.bin";
            }
            Path bridgeDir = Path.of("bridge");
            Files.createDirectories(bridgeDir);
            String fixName = Path.of(fixturePath).getFileName().toString();
            if (fixName.endsWith(".bin")) {
                fixName = fixName.substring(0, fixName.length() - 4);
            }
            sPath = bridgeDir.resolve("store_" + fixName + ".dat");
        }

        // If fixture given and store doesn't exist or is empty, ingest fixture
        if (fixturePath != null) {
            Path fPath = Path.of(fixturePath);
            if (!Files.exists(sPath) || Files.size(sPath) == 0) {
                System.err.printf("[JavaStoreBridge] Initializing store %s with fixture %s...%n", sPath, fPath);
                try (BlockStore store = BlockStore.open(sPath)) {
                    byte[] data = Files.readAllBytes(fPath);
                    int count = ingestEnvelopeStream(store, data);
                    store.sync();
                    System.err.printf("[JavaStoreBridge] Ingested %d events into store.%n", count);
                }
            }
        }

        // Open store and perform bounded retrieval
        RetrieveResult res;
        long tStart;
        long tEnd;
        try (BlockStore store = BlockStore.open(sPath)) {
            tStart = (startStr != null) ? parseTimeNs(startStr) : findMinTs(store);
            tEnd = (endStr != null) ? parseTimeNs(endStr) : findMaxTs(store, tStart);
            RetrieveRequest req = buildRequest(sourceStr, tStart, tEnd, maxEvents, eventType);
            System.err.printf("[JavaStoreBridge] Executing bounded retrieval: %s%n", req);
            res = store.retrieve(req);
            System.err.printf("[JavaStoreBridge] Retrieved %d events (truncated: %b)%n", res.events().size(), res.truncated());
        }

        // Resolve Python executable
        String python = resolvePython(pythonBin);

        // Prepare ProcessBuilder for bridge/pipeline_runner.py
        List<String> cmd = new ArrayList<>();
        cmd.add(python);
        cmd.add("bridge/pipeline_runner.py");
        cmd.add("--query"); cmd.add(query);
        cmd.add("--start"); cmd.add(Long.toUnsignedString(tStart));
        cmd.add("--end"); cmd.add(Long.toUnsignedString(tEnd));
        cmd.add("--source"); cmd.add(sourceStr);
        cmd.add("--max-events"); cmd.add(String.valueOf(maxEvents));
        cmd.add("--truncated"); cmd.add(String.valueOf(res.truncated()));
        cmd.add("--schema"); cmd.add(schemaPath);
        if (eventType != null) {
            cmd.add("--event-type"); cmd.add(String.valueOf(eventType));
        }

        System.err.printf("[JavaStoreBridge] Spawning Python pipeline: %s%n%n", String.join(" ", cmd));

        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.redirectOutput(ProcessBuilder.Redirect.INHERIT);
        pb.redirectError(ProcessBuilder.Redirect.INHERIT);

        Map<String, String> pbEnv = pb.environment();
        String currentDir = System.getProperty("user.dir");
        String existingPyPath = pbEnv.get("PYTHONPATH");
        pbEnv.put("PYTHONPATH", existingPyPath == null ? currentDir : currentDir + ":" + existingPyPath);

        Process proc = pb.start();

        // Feed retrieved envelopes through stdin pipe to Python
        try (OutputStream os = proc.getOutputStream()) {
            for (EventRecord ev : res.events()) {
                byte[] envelope = EnvelopeCodec.encodeEnvelope(ev);
                os.write(envelope);
            }
            os.flush();
        }

        int exitCode = proc.waitFor();
        if (exitCode != 0) {
            System.err.println("[JavaStoreBridge] Pipeline runner exited with code " + exitCode);
            System.exit(exitCode);
        }
    }

    private static RetrieveRequest buildRequest(String sourceStr, long tStart, long tEnd, int maxEvents, Integer eventType) {
        RetrieveRequest req;
        if (sourceStr == null || sourceStr.equalsIgnoreCase("ALL")) {
            req = RetrieveRequest.forAllSources(tStart, tEnd, maxEvents);
        } else {
            int sourceId = Integer.parseUnsignedInt(sourceStr);
            req = RetrieveRequest.forSource(sourceId, tStart, tEnd, maxEvents);
        }
        if (eventType != null) {
            req = req.withEventType(eventType);
        }
        return req;
    }

    private static long findMinTs(BlockStore store) {
        long min = Long.MAX_VALUE;
        boolean found = false;
        for (BlockInfo b : store.blocks()) {
            if (!b.empty() && b.recordCount() > 0) {
                if (Long.compareUnsigned(b.minTsNs(), min) < 0) {
                    min = b.minTsNs();
                    found = true;
                }
            }
        }
        return found ? min : 0L;
    }

    private static long findMaxTs(BlockStore store, long tStart) {
        long max = 0L;
        boolean found = false;
        for (BlockInfo b : store.blocks()) {
            if (!b.empty() && b.recordCount() > 0) {
                if (Long.compareUnsigned(b.maxTsNs(), max) > 0) {
                    max = b.maxTsNs();
                    found = true;
                }
            }
        }
        if (!found) {
            return tStart + 1_000_000_000L;
        }
        // Ensure tEnd > tStart
        if (Long.compareUnsigned(max, tStart) <= 0) {
            max = tStart + 1_000_000_000L;
        } else {
            max += 1_000_000_000L; // 1s buffer
        }
        // Cap window at MAX_WINDOW_NS
        if (Long.compareUnsigned(max - tStart, RetrieveRequest.MAX_WINDOW_NS) > 0) {
            max = tStart + RetrieveRequest.MAX_WINDOW_NS;
        }
        return max;
    }

    private static long parseTimeNs(String text) {
        text = text.trim();
        if (text.chars().allMatch(Character::isDigit)) {
            return Long.parseUnsignedLong(text);
        }
        Instant inst = Instant.parse(text);
        return inst.getEpochSecond() * 1_000_000_000L + inst.getNano();
    }

    private static Integer parseEventType(String text) {
        text = text.trim();
        if (text.startsWith("0x") || text.startsWith("0X")) {
            return Integer.parseInt(text.substring(2), 16);
        }
        return Integer.parseInt(text);
    }

    private static String resolvePython(String candidate) {
        if (candidate != null && Files.exists(Path.of(candidate))) {
            return candidate;
        }
        Path venvPy = Path.of(".venv/bin/python");
        if (Files.exists(venvPy)) {
            return venvPy.toAbsolutePath().toString();
        }
        Path venvPy3 = Path.of(".venv/bin/python3");
        if (Files.exists(venvPy3)) {
            return venvPy3.toAbsolutePath().toString();
        }
        return "python3";
    }
}
