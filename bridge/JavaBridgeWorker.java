package bridge;

import fss.core.BlockInfo;
import fss.core.BlockStore;
import fss.core.EnvelopeCodec;
import fss.core.EventRecord;
import fss.core.RetrieveRequest;
import fss.core.RetrieveResult;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.Channels;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Persistent Java Worker IPC over JEP-380 AF_UNIX Domain Sockets.
 *
 * Implements:
 *   - Stale socket unlinking on startup and shutdown.
 *   - Clean streams: System.out redirected to System.err.
 *   - Strict single-line UTF-8 NDJSON framed wire protocol.
 *   - Lossless uint64 serialization via Long.toUnsignedString.
 *   - W3C trace_id propagation into stderr logs and response headers.
 *   - Single-writer store safety via StoreLockManager.
 */
public final class JavaBridgeWorker {

    public static final String DEFAULT_SOCKET_PATH = "/tmp/fss_bridge.sock";
    private static final AtomicBoolean RUNNING = new AtomicBoolean(true);

    public static void main(String[] args) {
        // 1. Immediately redirect System.setOut to System.err
        System.setOut(System.err);

        String socketPathStr = DEFAULT_SOCKET_PATH;
        for (int i = 0; i < args.length; i++) {
            if ("--socket".equals(args[i]) && i + 1 < args.length) {
                socketPathStr = args[++i];
            }
        }

        Path socketPath = Path.of(socketPathStr).toAbsolutePath().normalize();
        startServer(socketPath);
    }

    public static void startServer(Path socketPath) {
        String initTraceId = UUID.randomUUID().toString().replace("-", "");
        logStructured(initTraceId, "INFO", "Initializing JavaBridgeWorker on AF_UNIX socket: " + socketPath);

        // Stale socket unlinking on startup
        try {
            Files.deleteIfExists(socketPath);
        } catch (IOException e) {
            logStructured(initTraceId, "WARN", "Could not delete existing socket file: " + e.getMessage());
        }

        ServerSocketChannel serverChannel;
        try {
            UnixDomainSocketAddress address = UnixDomainSocketAddress.of(socketPath);
            serverChannel = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            serverChannel.bind(address);
        } catch (IOException e) {
            logStructured(initTraceId, "ERROR", "Failed to bind Unix Domain Socket: " + e.getMessage());
            System.exit(1);
            return;
        }

        // Clean unlinking on JVM shutdown
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            RUNNING.set(false);
            try {
                serverChannel.close();
            } catch (Exception ignored) {}
            try {
                Files.deleteIfExists(socketPath);
            } catch (Exception ignored) {}
            logStructured(initTraceId, "INFO", "JavaBridgeWorker shutdown complete; socket unlinked.");
        }));

        logStructured(initTraceId, "INFO", "JavaBridgeWorker successfully bound and listening on " + socketPath);

        while (RUNNING.get()) {
            try {
                SocketChannel clientChannel = serverChannel.accept();
                if (clientChannel == null) continue;
                handleClient(clientChannel);
            } catch (Exception e) {
                if (RUNNING.get()) {
                    logStructured(initTraceId, "ERROR", "Error in accept loop: " + e.getMessage());
                }
            }
        }

        try {
            serverChannel.close();
            Files.deleteIfExists(socketPath);
        } catch (Exception ignored) {}
    }

    private static void handleClient(SocketChannel clientChannel) {
        try (clientChannel;
             BufferedReader reader = new BufferedReader(
                     Channels.newReader(clientChannel, StandardCharsets.UTF_8));
             BufferedWriter writer = new BufferedWriter(
                     Channels.newWriter(clientChannel, StandardCharsets.UTF_8))) {

            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) continue;

                Map<String, Object> req = parseJson(line);
                String action = (String) req.getOrDefault("action", "ping");
                String traceId = (String) req.getOrDefault("trace_id", UUID.randomUUID().toString().replace("-", ""));

                String responseJson;
                boolean shouldExit = false;

                switch (action.toLowerCase()) {
                    case "ping" -> responseJson = handlePing(traceId);
                    case "ingest" -> responseJson = handleIngest(req, traceId);
                    case "retrieve" -> responseJson = handleRetrieve(req, traceId);
                    case "bounds", "store_bounds" -> responseJson = handleBounds(req, traceId);
                    case "shutdown" -> {
                        responseJson = "{\"status\":\"OK\",\"message\":\"shutting down\",\"trace_id\":\"" + traceId + "\"}\n";
                        shouldExit = true;
                    }
                    default -> responseJson = "{\"status\":\"ERROR\",\"error_message\":\"Unknown action: " + action + "\",\"trace_id\":\"" + traceId + "\"}\n";
                }

                writer.write(responseJson);
                writer.flush();

                if (shouldExit) {
                    RUNNING.set(false);
                    new Thread(() -> {
                        try { Thread.sleep(100); } catch (Exception ignored) {}
                        System.exit(0);
                    }).start();
                    break;
                }
            }
        } catch (IOException e) {
            logStructured("worker", "DEBUG", "Client disconnected: " + e.getMessage());
        }
    }

    private static String handlePing(String traceId) {
        return "{\"status\":\"OK\",\"version\":\"1.0\",\"trace_id\":\"" + traceId + "\"}\n";
    }

    private static String handleIngest(Map<String, Object> req, String traceId) {
        String storePath = (String) req.get("store_path");
        if (storePath == null) {
            return "{\"status\":\"ERROR\",\"error\":\"store_path is required\",\"trace_id\":\"" + traceId + "\"}\n";
        }

        @SuppressWarnings("unchecked")
        List<String> fixtures = (List<String>) req.get("fixtures");
        if (fixtures == null || fixtures.isEmpty()) {
            return "{\"status\":\"ERROR\",\"error\":\"fixtures list is required\",\"trace_id\":\"" + traceId + "\"}\n";
        }

        Path sPath = Path.of(storePath);
        logStructured(traceId, "INFO", "Ingesting " + fixtures.size() + " fixtures into store " + sPath);

        int totalIngested = 0;
        try (AutoCloseable writeLock = StoreLockManager.acquireWriteLock(sPath)) {
            try (BlockStore store = StoreLockManager.openWithRetry(sPath, 3000)) {
                for (String fix : fixtures) {
                    Path fPath = Path.of(fix);
                    if (!Files.exists(fPath)) {
                        logStructured(traceId, "WARN", "Fixture file not found: " + fix);
                        continue;
                    }
                    byte[] data = Files.readAllBytes(fPath);
                    int count = ingestEnvelopeStream(store, data);
                    totalIngested += count;
                    logStructured(traceId, "INFO", "Ingested " + count + " events from " + fix);
                }
                store.sync();
            }
        } catch (Exception e) {
            logStructured(traceId, "ERROR", "Ingest failed: " + e.getMessage());
            return "{\"status\":\"ERROR\",\"error\":\"" + escapeJson(e.getMessage()) + "\",\"trace_id\":\"" + traceId + "\"}\n";
        }

        return "{\"status\":\"OK\",\"ingested_count\":" + totalIngested + ",\"trace_id\":\"" + traceId + "\"}\n";
    }

    private static String handleRetrieve(Map<String, Object> req, String traceId) {
        String storePath = (String) req.get("store_path");
        if (storePath == null) {
            return "{\"status\":\"ERROR\",\"error_message\":\"store_path is required\",\"trace_id\":\"" + traceId + "\",\"count\":0,\"truncated\":false,\"events\":[],\"data_gaps\":[]}\n";
        }

        Path sPath = Path.of(storePath);
        if (!Files.exists(sPath)) {
            return "{\"status\":\"ERROR\",\"error_message\":\"Store file does not exist: " + escapeJson(storePath) + "\",\"trace_id\":\"" + traceId + "\",\"count\":0,\"truncated\":false,\"events\":[],\"data_gaps\":[]}\n";
        }

        String sourceStr = (String) req.getOrDefault("source", "ALL");
        String startStr = (String) req.get("start_ns");
        String endStr = (String) req.get("end_ns");
        Number maxEventsNum = (Number) req.getOrDefault("max_events", 100);
        int maxEvents = maxEventsNum.intValue();

        Integer eventType = null;
        Object etObj = req.get("event_type");
        if (etObj instanceof Number n) {
            eventType = n.intValue();
        } else if (etObj instanceof String s && !s.isBlank()) {
            eventType = parseEventType(s);
        }

        RetrieveResult res;
        long tStart;
        long tEnd;
        long minTs;
        long maxTs;

        try (AutoCloseable readLock = StoreLockManager.acquireReadLock(sPath)) {
            try (BlockStore store = StoreLockManager.openWithRetry(sPath, 3000)) {
                minTs = findMinTs(store);
                maxTs = findMaxTs(store, minTs);
                tStart = (startStr != null) ? Long.parseUnsignedLong(startStr) : minTs;
                tEnd = (endStr != null) ? Long.parseUnsignedLong(endStr) : maxTs;

                if (Long.compareUnsigned(tEnd, tStart) <= 0) {
                    return String.format(
                        "{\"status\":\"EMPTY\",\"trace_id\":\"%s\",\"truncated\":false,\"count\":0,\"min_ts_ns\":\"%s\",\"max_ts_ns\":\"%s\",\"events\":[],\"data_gaps\":[]}\n",
                        traceId, Long.toUnsignedString(minTs), Long.toUnsignedString(maxTs)
                    );
                }

                RetrieveRequest retrieveReq = buildRequest(sourceStr, tStart, tEnd, maxEvents, eventType);
                logStructured(traceId, "INFO", "Executing retrieval: " + retrieveReq);
                res = store.retrieve(retrieveReq);
                logStructured(traceId, "INFO", "Retrieved " + res.events().size() + " events (truncated: " + res.truncated() + ")");
            }
        } catch (Exception e) {
            logStructured(traceId, "ERROR", "Retrieve error: " + e.getMessage());
            return "{\"status\":\"ERROR\",\"error_message\":\"" + escapeJson(e.getMessage()) + "\",\"trace_id\":\"" + traceId + "\",\"count\":0,\"truncated\":false,\"events\":[],\"data_gaps\":[]}\n";
        }

        return formatRetrievalEnvelopeJson(res, traceId, minTs, maxTs);
    }

    public static String formatRetrievalEnvelopeJson(RetrieveResult res, String traceId, long minTs, long maxTs) {
        StringBuilder sb = new StringBuilder(4096);
        boolean isEmpty = res.events().isEmpty();
        String status = isEmpty ? "EMPTY" : "OK";

        sb.append("{");
        sb.append("\"status\":\"").append(status).append("\",");
        sb.append("\"trace_id\":\"").append(traceId).append("\",");
        sb.append("\"truncated\":").append(res.truncated()).append(",");
        sb.append("\"count\":").append(res.events().size()).append(",");
        sb.append("\"min_ts_ns\":\"").append(Long.toUnsignedString(minTs)).append("\",");
        sb.append("\"max_ts_ns\":\"").append(Long.toUnsignedString(maxTs)).append("\",");

        // Events array
        sb.append("\"events\":[");
        Base64.Encoder b64 = Base64.getEncoder();
        for (int i = 0; i < res.events().size(); i++) {
            if (i > 0) sb.append(",");
            EventRecord ev = res.events().get(i);
            sb.append("{");
            sb.append("\"source_id\":").append(ev.sourceId()).append(",");
            sb.append("\"timestamp_ns\":\"").append(Long.toUnsignedString(ev.timestampNs())).append("\",");
            sb.append("\"event_type\":").append(ev.eventType()).append(",");
            sb.append("\"payload_b64\":\"").append(b64.encodeToString(ev.payload())).append("\"");
            sb.append("}");
        }
        sb.append("],");

        // Data gaps array
        sb.append("\"data_gaps\":[");
        if (res.truncated()) {
            sb.append("{");
            sb.append("\"source_id\":0,");
            sb.append("\"gap_start_ns\":\"").append(Long.toUnsignedString(minTs)).append("\",");
            sb.append("\"gap_end_ns\":\"").append(Long.toUnsignedString(maxTs)).append("\",");
            sb.append("\"reason\":\"TRUNCATED_MAX_EVENTS\"");
            sb.append("}");
        }
        sb.append("]");
        sb.append("}\n");

        return sb.toString();
    }

    private static String handleBounds(Map<String, Object> req, String traceId) {
        String storePath = (String) req.get("store_path");
        if (storePath == null) {
            return "{\"status\":\"ERROR\",\"error_message\":\"store_path is required\",\"trace_id\":\"" + traceId + "\"}\n";
        }

        Path sPath = Path.of(storePath);
        if (!Files.exists(sPath)) {
            return "{\"status\":\"ERROR\",\"error_message\":\"Store file does not exist: " + escapeJson(storePath) + "\",\"trace_id\":\"" + traceId + "\"}\n";
        }

        Path rootDir = Files.isDirectory(sPath) ? sPath : sPath.getParent();
        if (rootDir != null && (Files.exists(rootDir.resolve("block-metadata.dat")) || Files.exists(rootDir.resolve("segments")))) {
            // Segmented store layout
            Path blocksDat = rootDir.resolve("blocks.dat");
            Path metaDat = rootDir.resolve("block-metadata.dat");
            Path envCfg = rootDir.resolve("envelope.cfg");
            if (!Files.exists(envCfg)) {
                envCfg = Path.of("fss-core/envelope.cfg").toAbsolutePath();
            }
            try {
                Storage.EnvelopeConfig env = Files.exists(envCfg)
                        ? Storage.EnvelopeConfig.load(envCfg, ByteOrder.BIG_ENDIAN)
                        : null;
                Storage.StorageConfig cfg = new Storage.StorageConfig(
                        rootDir, 65536, 1048576, 4096, 60000, env
                );
                Storage.BlockStore bStore = new Storage.BlockStore(blocksDat);
                Storage.BlockMetadataStore mStore = new Storage.BlockMetadataStore(metaDat, 65536);
                Storage.SegmentMetadataStore sStore = new Storage.SegmentMetadataStore(rootDir);
                try {
                    Retrieval.RetrievalService rService = new Retrieval.RetrievalService(cfg, bStore, mStore, sStore);
                    Retrieval.StoreBounds bounds = rService.discoverBounds();
                    StringBuilder sb = new StringBuilder();
                    sb.append("{\"status\":\"").append(bounds.status()).append("\",");
                    sb.append("\"trace_id\":\"").append(traceId).append("\",");
                    if (bounds.min_ts_ns() != null) {
                        sb.append("\"min_ts_ns\":\"").append(bounds.min_ts_ns()).append("\",");
                        sb.append("\"max_ts_ns\":\"").append(bounds.max_ts_ns()).append("\",");
                    }
                    sb.append("\"segment_count\":").append(bounds.segment_count()).append(",");
                    sb.append("\"block_count\":").append(bounds.block_count()).append(",");
                    sb.append("\"segment_windows\":[");
                    for (int j = 0; j < bounds.segment_windows().size(); j++) {
                        Retrieval.StoreBounds.SegmentWindow sw = bounds.segment_windows().get(j);
                        if (j > 0) sb.append(",");
                        sb.append("{\"start_ts_ns\":\"").append(sw.start_ts_ns()).append("\",")
                          .append("\"end_ts_ns\":\"").append(sw.end_ts_ns()).append("\",")
                          .append("\"block_count\":").append(sw.block_count()).append("}");
                    }
                    sb.append("],\"known_sources\":[");
                    for (int j = 0; j < bounds.known_sources().size(); j++) {
                        if (j > 0) sb.append(",");
                        sb.append(bounds.known_sources().get(j));
                    }
                    sb.append("]}\n");
                    return sb.toString();
                } finally {
                    mStore.close();
                    bStore.close();
                }
            } catch (Exception ex) {
                return "{\"status\":\"ERROR\",\"error_message\":\"" + escapeJson(ex.getMessage()) + "\",\"trace_id\":\"" + traceId + "\"}\n";
            }
        }

        // Single-file store
        try (AutoCloseable readLock = StoreLockManager.acquireReadLock(sPath)) {
            try (BlockStore store = StoreLockManager.openWithRetry(sPath, 3000)) {
                List<BlockInfo> blocks = store.blocks();
                long minTs = -1L;
                long maxTs = 0L;
                boolean found = false;
                int nonEmpBlocks = 0;
                for (BlockInfo b : blocks) {
                    if (!b.empty() && b.recordCount() > 0) {
                        nonEmpBlocks++;
                        if (!found || Long.compareUnsigned(b.minTsNs(), minTs) < 0) minTs = b.minTsNs();
                        if (!found || Long.compareUnsigned(b.maxTsNs(), maxTs) > 0) maxTs = b.maxTsNs();
                        found = true;
                    }
                }
                StringBuilder sb = new StringBuilder();
                if (found && Long.compareUnsigned(minTs, maxTs) <= 0) {
                    long maxTsQuery = (maxTs == -1L) ? -1L : maxTs + 1L;
                    sb.append("{\"status\":\"OK\",\"trace_id\":\"").append(traceId).append("\",");
                    sb.append("\"min_ts_ns\":\"").append(Long.toUnsignedString(minTs)).append("\",");
                    sb.append("\"max_ts_ns\":\"").append(Long.toUnsignedString(maxTsQuery)).append("\",");
                    sb.append("\"segment_count\":1,\"block_count\":").append(blocks.size()).append(",");
                    sb.append("\"segment_windows\":[{\"start_ts_ns\":\"").append(Long.toUnsignedString(minTs))
                      .append("\",\"end_ts_ns\":\"").append(Long.toUnsignedString(maxTsQuery))
                      .append("\",\"block_count\":").append(nonEmpBlocks).append("}],");
                    sb.append("\"known_sources\":[]}\n");
                } else {
                    sb.append("{\"status\":\"EMPTY\",\"trace_id\":\"").append(traceId).append("\",");
                    sb.append("\"segment_count\":0,\"block_count\":").append(blocks.size()).append(",");
                    sb.append("\"segment_windows\":[],\"known_sources\":[]}\n");
                }
                return sb.toString();
            }
        } catch (Exception ex) {
            return "{\"status\":\"ERROR\",\"error_message\":\"" + escapeJson(ex.getMessage()) + "\",\"trace_id\":\"" + traceId + "\"}\n";
        }
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
                System.err.println("WARN: Truncated trailing envelope at offset " + off + " (quarantined " + (data.length - off) + " bytes); preserving valid prefix");
                break;
            }
            EventRecord record = EnvelopeCodec.decodeEnvelope(buf, off, recordLen);
            store.append(record);
            count++;
            off += recordLen;
        }
        return count;
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
        long min = -1L;
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

    private static Integer parseEventType(String text) {
        text = text.trim();
        if (text.startsWith("0x") || text.startsWith("0X")) {
            return Integer.parseInt(text.substring(2), 16);
        }
        return Integer.parseInt(text);
    }

    public static void logStructured(String traceId, String level, String message) {
        String ts = Instant.now().toString();
        System.err.printf("{\"timestamp\":\"%s\",\"trace_id\":\"%s\",\"level\":\"%s\",\"message\":\"%s\"}%n",
                ts, traceId, level, escapeJson(message));
    }

    private static String escapeJson(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t");
    }

    /**
     * Minimal recursive-descent JSON parser for flat/nested maps and lists.
     */
    public static Map<String, Object> parseJson(String json) {
        json = json.trim();
        if (!json.startsWith("{") || !json.endsWith("}")) {
            return new HashMap<>();
        }
        JsonParser p = new JsonParser(json);
        Object obj = p.parseValue();
        if (obj instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) obj;
            return m;
        }
        return new HashMap<>();
    }

    private static class JsonParser {
        private final String src;
        private int idx = 0;

        JsonParser(String src) {
            this.src = src;
        }

        private void skipWhitespace() {
            while (idx < src.length() && Character.isWhitespace(src.charAt(idx))) {
                idx++;
            }
        }

        Object parseValue() {
            skipWhitespace();
            if (idx >= src.length()) return null;
            char c = src.charAt(idx);
            if (c == '{') return parseObject();
            if (c == '[') return parseArray();
            if (c == '"') return parseString();
            if (c == 't' || c == 'f') return parseBoolean();
            if (c == 'n') return parseNull();
            return parseNumber();
        }

        private Map<String, Object> parseObject() {
            Map<String, Object> map = new HashMap<>();
            idx++; // skip '{'
            skipWhitespace();
            if (idx < src.length() && src.charAt(idx) == '}') {
                idx++;
                return map;
            }
            while (idx < src.length()) {
                skipWhitespace();
                String key = parseString();
                skipWhitespace();
                if (idx < src.length() && src.charAt(idx) == ':') {
                    idx++;
                }
                Object val = parseValue();
                map.put(key, val);
                skipWhitespace();
                if (idx < src.length() && src.charAt(idx) == ',') {
                    idx++;
                } else if (idx < src.length() && src.charAt(idx) == '}') {
                    idx++;
                    break;
                }
            }
            return map;
        }

        private List<Object> parseArray() {
            List<Object> list = new ArrayList<>();
            idx++; // skip '['
            skipWhitespace();
            if (idx < src.length() && src.charAt(idx) == ']') {
                idx++;
                return list;
            }
            while (idx < src.length()) {
                Object val = parseValue();
                list.add(val);
                skipWhitespace();
                if (idx < src.length() && src.charAt(idx) == ',') {
                    idx++;
                } else if (idx < src.length() && src.charAt(idx) == ']') {
                    idx++;
                    break;
                }
            }
            return list;
        }

        private String parseString() {
            if (idx >= src.length() || src.charAt(idx) != '"') return "";
            idx++; // skip opening quote
            StringBuilder sb = new StringBuilder();
            while (idx < src.length()) {
                char c = src.charAt(idx++);
                if (c == '"') break;
                if (c == '\\' && idx < src.length()) {
                    char esc = src.charAt(idx++);
                    switch (esc) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'u' -> {
                            if (idx + 4 <= src.length()) {
                                String hex = src.substring(idx, idx + 4);
                                sb.append((char) Integer.parseInt(hex, 16));
                                idx += 4;
                            }
                        }
                        default -> sb.append(esc);
                    }
                } else {
                    sb.append(c);
                }
            }
            return sb.toString();
        }

        private Boolean parseBoolean() {
            if (src.startsWith("true", idx)) {
                idx += 4;
                return Boolean.TRUE;
            }
            if (src.startsWith("false", idx)) {
                idx += 5;
                return Boolean.FALSE;
            }
            return Boolean.FALSE;
        }

        private Object parseNull() {
            if (src.startsWith("null", idx)) {
                idx += 4;
            }
            return null;
        }

        private Number parseNumber() {
            int start = idx;
            if (idx < src.length() && (src.charAt(idx) == '-' || src.charAt(idx) == '+')) {
                idx++;
            }
            while (idx < src.length() && (Character.isDigit(src.charAt(idx)) || src.charAt(idx) == '.' || src.charAt(idx) == 'e' || src.charAt(idx) == 'E' || src.charAt(idx) == '-')) {
                idx++;
            }
            String numStr = src.substring(start, idx);
            if (numStr.contains(".") || numStr.contains("e") || numStr.contains("E")) {
                try {
                    return Double.parseDouble(numStr);
                } catch (Exception e) {
                    return 0.0;
                }
            }
            try {
                return Long.parseLong(numStr);
            } catch (Exception e) {
                return 0L;
            }
        }
    }
}
