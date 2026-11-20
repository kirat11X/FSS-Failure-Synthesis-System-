package bridge;

import fss.core.BlockInfo;
import fss.core.BlockStore;
import fss.core.EnvelopeCodec;
import fss.core.EventRecord;
import fss.core.RetrieveRequest;
import fss.core.RetrieveResult;

import java.io.BufferedOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

/**
 * Non-invasive Java-to-Python bridge CLI and worker entrypoint.
 *
 * Implements:
 *   - Clean streams: redirects System.out to System.err; captures raw protocol OutputStream.
 *   - Framed wire protocol: single-line UTF-8 NDJSON and legacy binary envelope streaming.
 *   - Concurrency & locking: in-JVM ReentrantReadWriteLock + FileChannel.lock() on writes.
 *   - uint64 integrity: Long.toUnsignedString and Long.parseUnsignedLong.
 *   - W3C 32-hex trace_id propagation.
 */
public final class JavaStoreBridge {

    public static void main(String[] args) {
        // 1. Capture raw FileDescriptor.out for protocol frames
        OutputStream protocolOut = new BufferedOutputStream(new FileOutputStream(FileDescriptor.out));
        // 2. Immediately redirect System.setOut to System.err so stray logs or JVM banners cannot corrupt frames
        System.setOut(System.err);

        if (args.length == 0 || args[0].equals("-h") || args[0].equals("--help") || args[0].equals("help")) {
            printHelp(protocolOut);
            return;
        }

        String command = args[0].toLowerCase();
        try {
            switch (command) {
                case "ingest" -> runIngest(args, protocolOut);
                case "retrieve" -> runRetrieve(args, protocolOut);
                case "worker" -> runWorker(args);
                case "synthesize", "pipeline" -> runPipeline(args, protocolOut);
                default -> {
                    System.err.println("Unknown command: " + command);
                    printHelp(protocolOut);
                    System.exit(1);
                }
            }
        } catch (Exception ex) {
            System.err.println("Error executing " + command + ": " + ex.getMessage());
            ex.printStackTrace(System.err);
            System.exit(2);
        }
    }

    private static void printHelp(OutputStream out) {
        try {
            PrintStream ps = new PrintStream(out, true, StandardCharsets.UTF_8);
            ps.println("JavaStoreBridge: FSS Java Core & Python Pipeline Linkage");
            ps.println("Usage: java -cp <classpath> bridge.JavaStoreBridge <command> [options]");
            ps.println();
            ps.println("Commands:");
            ps.println("  ingest      Ingest wire envelope (.bin) fixtures into a BlockStore data file");
            ps.println("              Options: --store <path> [--fixture <bin_path>...] [--trace-id <id>]");
            ps.println();
            ps.println("  retrieve    Retrieve bounded events from BlockStore");
            ps.println("              Options: --store <path> [--source <ALL|id>] [--start <ns|ISO>]");
            ps.println("                       [--end <ns|ISO>] [--event-type <id>] [--max-events <n>]");
            ps.println("                       [--format <ndjson|binary>] [--trace-id <id>]");
            ps.println();
            ps.println("  worker      Launch persistent AF_UNIX domain socket worker");
            ps.println("              Options: [--socket <path>]");
            ps.println();
            ps.println("  pipeline    Execute end-to-end retrieval, evidence decoding & failure synthesis");
            ps.println("  synthesize  Options: [--store <path>] [--fixture <bin_path>] [--query <text>]");
            ps.println("                       [--source <ALL|id>] [--start <ns|ISO>] [--end <ns|ISO>]");
            ps.println("                       [--max-events <n>] [--python <bin>] [--schema <yaml>]");
            ps.flush();
        } catch (Exception ignored) {}
    }

    private static Path findProjectRoot() {
        // 1. Check FSS_ROOT environment variable
        String envRoot = System.getenv("FSS_ROOT");
        if (envRoot != null && !envRoot.isBlank()) {
            Path p = Path.of(envRoot).toAbsolutePath().normalize();
            if (Files.exists(p.resolve("schemas/drone_v0.yaml")) && Files.exists(p.resolve("bridge"))) {
                return p;
            }
        }

        // 2. Check current working directory
        Path currentDir = Path.of(System.getProperty("user.dir")).toAbsolutePath().normalize();
        if (Files.exists(currentDir.resolve("schemas/drone_v0.yaml")) && Files.exists(currentDir.resolve("bridge"))) {
            return currentDir;
        }

        // 3. Check code source location of JavaStoreBridge.class
        try {
            var codeSource = JavaStoreBridge.class.getProtectionDomain().getCodeSource();
            if (codeSource != null && codeSource.getLocation() != null) {
                Path loc = Path.of(codeSource.getLocation().toURI()).toAbsolutePath().normalize();
                Path cur = loc;
                while (cur != null) {
                    if (Files.exists(cur.resolve("schemas/drone_v0.yaml")) && Files.exists(cur.resolve("bridge"))) {
                        return cur;
                    }
                    cur = cur.getParent();
                }
            }
        } catch (Exception ignored) {
        }

        return currentDir;
    }

    private static String extractTraceId(String[] args) {
        for (int i = 0; i < args.length; i++) {
            if ("--trace-id".equalsIgnoreCase(args[i]) && i + 1 < args.length) {
                return args[i + 1].trim();
            }
        }
        String envId = System.getenv("FSS_TRACE_ID");
        if (envId != null && !envId.isBlank()) {
            return envId.trim();
        }
        return UUID.randomUUID().toString().replace("-", "");
    }

    private static void runIngest(String[] args, OutputStream protocolOut) throws IOException {
        String storePath = null;
        List<String> fixtures = new ArrayList<>();
        String traceId = extractTraceId(args);

        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--store" -> storePath = args[++i];
                case "--fixture" -> fixtures.add(args[++i]);
                case "--trace-id" -> traceId = args[++i];
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

        Path projectRoot = findProjectRoot();
        Path sPath = Path.of(storePath);
        if (!sPath.isAbsolute()) {
            sPath = Files.exists(sPath) ? sPath.toAbsolutePath() : projectRoot.resolve(sPath);
        }

        int totalIngested = 0;
        try (AutoCloseable writeLock = StoreLockManager.acquireWriteLock(sPath)) {
            try (BlockStore store = StoreLockManager.openWithRetry(sPath, 3000)) {
                for (String f : fixtures) {
                    Path fPath = Path.of(f);
                    if (!fPath.isAbsolute()) {
                        fPath = Files.exists(fPath) ? fPath.toAbsolutePath() : projectRoot.resolve(fPath);
                    }
                    if (!Files.exists(fPath)) {
                        JavaBridgeWorker.logStructured(traceId, "WARN", "Fixture file not found: " + f);
                        continue;
                    }
                    byte[] data = Files.readAllBytes(fPath);
                    int count = ingestEnvelopeStream(store, data);
                    JavaBridgeWorker.logStructured(traceId, "INFO", String.format("Ingested %d events from %s", count, f));
                    totalIngested += count;
                }
                store.sync();
            }
        } catch (Exception e) {
            JavaBridgeWorker.logStructured(traceId, "ERROR", "Ingest error: " + e.getMessage());
            throw new IOException("Ingest failed: " + e.getMessage(), e);
        }

        String msg = String.format("Successfully ingested %d events into BlockStore at %s%n", totalIngested, sPath);
        protocolOut.write(msg.getBytes(StandardCharsets.UTF_8));
        protocolOut.flush();
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

    private static void runRetrieve(String[] args, OutputStream protocolOut) throws IOException {
        String storePath = null;
        String sourceStr = "ALL";
        String startStr = null;
        String endStr = null;
        Integer eventType = null;
        int maxEvents = 100;
        String format = "binary"; // Default for backwards compatibility
        String traceId = extractTraceId(args);

        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--store" -> storePath = args[++i];
                case "--source" -> sourceStr = args[++i];
                case "--start" -> startStr = args[++i];
                case "--end" -> endStr = args[++i];
                case "--event-type" -> eventType = parseEventType(args[++i]);
                case "--max-events" -> maxEvents = Integer.parseInt(args[++i]);
                case "--format" -> format = args[++i].toLowerCase();
                case "--ndjson" -> format = "ndjson";
                case "--trace-id" -> traceId = args[++i];
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

        Path projectRoot = findProjectRoot();
        Path sPath = Path.of(storePath);
        if (!sPath.isAbsolute()) {
            sPath = Files.exists(sPath) ? sPath.toAbsolutePath() : projectRoot.resolve(sPath);
        }
        if (!Files.exists(sPath)) {
            System.err.println("Error: Store file not found: " + sPath);
            System.exit(1);
        }

        try (AutoCloseable readLock = StoreLockManager.acquireReadLock(sPath)) {
            try (BlockStore store = StoreLockManager.openWithRetry(sPath, 3000)) {
                long minTs = findMinTs(store);
                long maxTs = findMaxTs(store, minTs);
                long tStart = (startStr != null) ? parseTimeNs(startStr) : minTs;
                long tEnd = (endStr != null) ? parseTimeNs(endStr) : maxTs;

                if (Long.compareUnsigned(tEnd, tStart) <= 0) {
                    if ("ndjson".equalsIgnoreCase(format)) {
                        String envelopeJson = String.format(
                            "{\"status\":\"EMPTY\",\"trace_id\":\"%s\",\"truncated\":false,\"count\":0,\"min_ts_ns\":\"%s\",\"max_ts_ns\":\"%s\",\"events\":[],\"data_gaps\":[]}\n",
                            traceId, Long.toUnsignedString(minTs), Long.toUnsignedString(maxTs)
                        );
                        protocolOut.write(envelopeJson.getBytes(StandardCharsets.UTF_8));
                        protocolOut.flush();
                    } else {
                        String header = "{\"truncated\": false, \"count\": 0}\n";
                        protocolOut.write(header.getBytes(StandardCharsets.UTF_8));
                        protocolOut.flush();
                    }
                    return;
                }

                RetrieveRequest req = buildRequest(sourceStr, tStart, tEnd, maxEvents, eventType);
                RetrieveResult res = store.retrieve(req);
                JavaBridgeWorker.logStructured(traceId, "INFO", String.format("Retrieved %d events (truncated: %b)", res.events().size(), res.truncated()));

                if ("ndjson".equalsIgnoreCase(format)) {
                    String envelopeJson = JavaBridgeWorker.formatRetrievalEnvelopeJson(res, traceId, minTs, maxTs);
                    protocolOut.write(envelopeJson.getBytes(StandardCharsets.UTF_8));
                } else {
                    // Legacy binary envelope streaming
                    String header = String.format("{\"truncated\": %b, \"count\": %d}\n", res.truncated(), res.events().size());
                    protocolOut.write(header.getBytes(StandardCharsets.UTF_8));
                    for (EventRecord ev : res.events()) {
                        byte[] envelope = EnvelopeCodec.encodeEnvelope(ev);
                        protocolOut.write(envelope);
                    }
                }
                protocolOut.flush();
            }
        } catch (Exception e) {
            JavaBridgeWorker.logStructured(traceId, "ERROR", "Retrieve failed: " + e.getMessage());
            throw new IOException("Retrieve error: " + e.getMessage(), e);
        }
    }

    private static void runWorker(String[] args) {
        JavaBridgeWorker.main(args);
    }

    private static void runPipeline(String[] args, OutputStream protocolOut) throws Exception {
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
        String jsonOutFile = null;
        boolean jsonOut = false;
        String traceId = extractTraceId(args);

        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--store" -> storePath = args[++i];
                case "--fixture" -> fixturePath = args[++i];
                case "--scenario" -> {
                    String sc = args[++i];
                    if (!sc.endsWith(".bin")) {
                        sc = sc + ".bin";
                    }
                    fixturePath = sc.startsWith("fixtures/") ? sc : "fixtures/golden/" + sc;
                }
                case "--query" -> query = args[++i];
                case "--source" -> sourceStr = args[++i];
                case "--start" -> startStr = args[++i];
                case "--end" -> endStr = args[++i];
                case "--event-type" -> eventType = parseEventType(args[++i]);
                case "--max-events" -> maxEvents = Integer.parseInt(args[++i]);
                case "--python" -> pythonBin = args[++i];
                case "--schema" -> schemaPath = args[++i];
                case "--trace-id" -> traceId = args[++i];
                case "--json-out" -> {
                    if (i + 1 < args.length && !args[i + 1].startsWith("--")) {
                        jsonOutFile = args[++i];
                    } else {
                        jsonOut = true;
                    }
                }
            }
        }

        Path projectRoot = findProjectRoot();

        // Auto-provision store if fixture provided or default
        Path sPath;
        if (storePath != null) {
            sPath = Path.of(storePath);
            if (!sPath.isAbsolute()) {
                sPath = Files.exists(sPath) ? sPath.toAbsolutePath() : projectRoot.resolve(sPath);
            }
        } else {
            if (fixturePath == null) {
                fixturePath = "fixtures/golden/scenario_298_474.bin";
            }
            Path bridgeDir = projectRoot.resolve("bridge");
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
            if (!fPath.isAbsolute()) {
                fPath = Files.exists(fPath) ? fPath.toAbsolutePath() : projectRoot.resolve(fPath);
            }
            if (!Files.exists(sPath) || Files.size(sPath) == 0) {
                JavaBridgeWorker.logStructured(traceId, "INFO", String.format("Initializing store %s with fixture %s...", sPath, fPath));
                try (AutoCloseable writeLock = StoreLockManager.acquireWriteLock(sPath)) {
                    try (BlockStore store = StoreLockManager.openWithRetry(sPath, 3000)) {
                        byte[] data = Files.readAllBytes(fPath);
                        int count = ingestEnvelopeStream(store, data);
                        store.sync();
                        JavaBridgeWorker.logStructured(traceId, "INFO", String.format("Ingested %d events into store.", count));
                    }
                }
            }
        }

        // Open store and perform bounded retrieval
        RetrieveResult res;
        long tStart;
        long tEnd;
        try (AutoCloseable readLock = StoreLockManager.acquireReadLock(sPath)) {
            try (BlockStore store = StoreLockManager.openWithRetry(sPath, 3000)) {
                tStart = (startStr != null) ? parseTimeNs(startStr) : findMinTs(store);
                tEnd = (endStr != null) ? parseTimeNs(endStr) : findMaxTs(store, tStart);
                RetrieveRequest req = buildRequest(sourceStr, tStart, tEnd, maxEvents, eventType);
                JavaBridgeWorker.logStructured(traceId, "INFO", String.format("Executing bounded retrieval: %s", req));
                res = store.retrieve(req);
                JavaBridgeWorker.logStructured(traceId, "INFO", String.format("Retrieved %d events (truncated: %b)", res.events().size(), res.truncated()));
            }
        }

        // Resolve Python executable
        String python = resolvePython(pythonBin, projectRoot);

        // Resolve schema path
        Path scPath = Path.of(schemaPath);
        if (!scPath.isAbsolute()) {
            scPath = Files.exists(scPath) ? scPath.toAbsolutePath() : projectRoot.resolve(scPath);
        }

        // Prepare ProcessBuilder for bridge/pipeline_runner.py
        Path runnerPath = projectRoot.resolve("bridge/pipeline_runner.py");
        List<String> cmd = new ArrayList<>();
        cmd.add(python);
        cmd.add(runnerPath.toAbsolutePath().toString());
        cmd.add("--query"); cmd.add(query);
        cmd.add("--start"); cmd.add(Long.toUnsignedString(tStart));
        cmd.add("--end"); cmd.add(Long.toUnsignedString(tEnd));
        cmd.add("--source"); cmd.add(sourceStr);
        cmd.add("--max-events"); cmd.add(String.valueOf(maxEvents));
        cmd.add("--truncated"); cmd.add(String.valueOf(res.truncated()));
        cmd.add("--schema"); cmd.add(scPath.toAbsolutePath().toString());
        if (eventType != null) {
            cmd.add("--event-type"); cmd.add(String.valueOf(eventType));
        }
        if (jsonOutFile != null) {
            cmd.add("--json-out"); cmd.add(jsonOutFile);
        } else if (jsonOut) {
            cmd.add("--json-out");
        }

        JavaBridgeWorker.logStructured(traceId, "INFO", String.format("Spawning Python pipeline: %s", String.join(" ", cmd)));

        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.directory(projectRoot.toFile());
        pb.redirectOutput(ProcessBuilder.Redirect.INHERIT);
        pb.redirectError(ProcessBuilder.Redirect.INHERIT);

        Map<String, String> pbEnv = pb.environment();
        String rootStr = projectRoot.toAbsolutePath().toString();
        String existingPyPath = pbEnv.get("PYTHONPATH");
        pbEnv.put("PYTHONPATH", existingPyPath == null ? rootStr : rootStr + ":" + existingPyPath);
        pbEnv.put("FSS_TRACE_ID", traceId);

        Process proc = pb.start();

        // Feed retrieved envelopes through stdin pipe to Python
        try (OutputStream os = proc.getOutputStream()) {
            for (EventRecord ev : res.events()) {
                byte[] envelope = EnvelopeCodec.encodeEnvelope(ev);
                os.write(envelope);
            }
            os.flush();
        } catch (IOException ioe) {
            JavaBridgeWorker.logStructured(traceId, "WARN", "Notice: Python process closed stdin: " + ioe.getMessage());
        }

        boolean finished = false;
        int exitCode = 1;
        try {
            finished = proc.waitFor(300, TimeUnit.SECONDS);
            if (!finished) {
                JavaBridgeWorker.logStructured(traceId, "ERROR", "Python pipeline timed out after 300 seconds, terminating forcibly...");
                proc.destroyForcibly();
                proc.waitFor(5, TimeUnit.SECONDS);
                exitCode = 124;
            } else {
                exitCode = proc.exitValue();
            }
        } catch (InterruptedException ie) {
            JavaBridgeWorker.logStructured(traceId, "ERROR", "Interrupted while waiting for Python pipeline, terminating forcibly...");
            proc.destroyForcibly();
            Thread.currentThread().interrupt();
            exitCode = 130;
        }

        if (exitCode != 0) {
            JavaBridgeWorker.logStructured(traceId, "ERROR", "Pipeline runner exited with code " + exitCode);
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
        long min = -1L; // 0xFFFFFFFFFFFFFFFFL unsigned max
        boolean found = false;
        for (BlockInfo b : store.blocks()) {
            if (!b.empty() && b.recordCount() > 0) {
                if (!found || Long.compareUnsigned(b.minTsNs(), min) < 0) {
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
                if (!found || Long.compareUnsigned(b.maxTsNs(), max) > 0) {
                    max = b.maxTsNs();
                    found = true;
                }
            }
        }
        if (!found) {
            long delta = 1_000_000_000L;
            if (Long.compareUnsigned(-1L - tStart, delta) < 0) {
                return -1L;
            }
            return tStart + delta;
        }
        if (Long.compareUnsigned(max, tStart) <= 0) {
            long delta = 1_000_000_000L;
            if (Long.compareUnsigned(-1L - tStart, delta) < 0) {
                return -1L;
            }
            max = tStart + delta;
        } else {
            long delta = 1_000_000_000L;
            if (Long.compareUnsigned(-1L - max, delta) >= 0) {
                max += delta;
            } else {
                max = -1L;
            }
        }
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

    private static String resolvePython(String candidate, Path projectRoot) {
        if (candidate != null && !candidate.isBlank()) {
            Path candPath = Path.of(candidate);
            if (!candPath.isAbsolute()) {
                Path fromRoot = projectRoot.resolve(candPath);
                if (Files.exists(fromRoot)) {
                    return fromRoot.toAbsolutePath().toString();
                }
            }
            if (Files.exists(candPath) || canExecute(candidate)) {
                return candidate;
            }
        }

        Path venvPy = projectRoot.resolve(".venv/bin/python");
        if (Files.exists(venvPy) && Files.isExecutable(venvPy)) {
            return venvPy.toAbsolutePath().toString();
        }
        Path venvPy3 = projectRoot.resolve(".venv/bin/python3");
        if (Files.exists(venvPy3) && Files.isExecutable(venvPy3)) {
            return venvPy3.toAbsolutePath().toString();
        }

        String envPython = System.getenv("PYTHON");
        if (envPython != null && !envPython.isBlank()) {
            Path ep = Path.of(envPython);
            if (Files.exists(ep) || canExecute(envPython)) {
                return envPython;
            }
        }

        String envPath = System.getenv("PATH");
        if (envPath != null) {
            for (String dir : envPath.split(java.io.File.pathSeparator)) {
                if (!dir.isBlank()) {
                    Path p3 = Path.of(dir).resolve("python3");
                    if (Files.isExecutable(p3)) {
                        return p3.toAbsolutePath().toString();
                    }
                    Path p = Path.of(dir).resolve("python");
                    if (Files.isExecutable(p)) {
                        return p.toAbsolutePath().toString();
                    }
                }
            }
        }

        if (canExecute("python3")) {
            return "python3";
        }
        if (canExecute("python")) {
            return "python";
        }

        return "python3";
    }

    private static boolean canExecute(String bin) {
        try {
            Process p = new ProcessBuilder(bin, "--version")
                    .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                    .redirectError(ProcessBuilder.Redirect.DISCARD)
                    .start();
            return p.waitFor(3, TimeUnit.SECONDS) && p.exitValue() == 0;
        } catch (Exception e) {
            return false;
        }
    }
}
