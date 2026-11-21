package Retrieval;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import fss.core.BlockInfo;
import fss.core.BlockStore;
import fss.core.EnvelopeCodec;
import fss.core.EventRecord;
import fss.core.RetrieveRequest;
import fss.core.RetrieveResult;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.Channels;
import java.nio.channels.FileChannel;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.locks.ReentrantReadWriteLock;

/**
 * Persistent JEP-380 AF_UNIX Domain Socket Bridge Server (REQ-072).
 *
 * Implements:
 *  - Persistent AF_UNIX daemon on default /tmp/fss_bridge.sock (or CLI/env var).
 *  - Clean socket file unlinking before bind and on JVM shutdown.
 *  - Diagnostic logs strictly to System.err with ISO-8601 timestamps and trace_id.
 *  - 4-byte Big-Endian length prefixed frame protocol (up to 16 MB) + graceful NDJSON acceptance.
 *  - Commands: ping, retrieve, retrieve_full, shutdown.
 *  - Lossless uint64 serialization via Long.toUnsignedString.
 *  - Safe store access with ReentrantReadWriteLock per store path.
 */
public final class UdsBridgeServer {

    public static final String DEFAULT_SOCKET_PATH = "/tmp/fss_bridge.sock";
    public static final int MAX_FRAME_SIZE = 16 * 1024 * 1024; // 16 MB

    private static final AtomicBoolean RUNNING = new AtomicBoolean(true);
    private static final ConcurrentHashMap<String, ReentrantReadWriteLock> STORE_LOCKS = new ConcurrentHashMap<>();
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final HexFormat HEX = HexFormat.of();
    private static final Base64.Encoder B64 = Base64.getEncoder();

    private final Path socketPath;
    private ServerSocketChannel serverChannel;
    private final ExecutorService clientExecutor = Executors.newCachedThreadPool();

    public UdsBridgeServer(Path socketPath) {
        this.socketPath = socketPath.toAbsolutePath().normalize();
    }

    public static void log(String traceId, String level, String message) {
        String ts = Instant.now().toString();
        String tid = (traceId != null && !traceId.isBlank()) ? traceId : "main";
        System.err.println(ts + " [" + level + "] [" + tid + "] " + message);
    }

    private static ReentrantReadWriteLock getLock(Path storePath) {
        String key = storePath.toAbsolutePath().normalize().toString();
        return STORE_LOCKS.computeIfAbsent(key, k -> new ReentrantReadWriteLock(true));
    }

    private static BlockStore openWithRetry(Path storePath, int timeoutMs) throws IOException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (true) {
            try {
                return BlockStore.open(storePath);
            } catch (IOException ioe) {
                if (ioe.getMessage() != null && ioe.getMessage().contains("locked by another process")
                        && System.currentTimeMillis() < deadline) {
                    try {
                        Thread.sleep(20);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        throw ioe;
                    }
                    continue;
                }
                throw ioe;
            }
        }
    }

    public void start() throws IOException {
        String initTraceId = UUID.randomUUID().toString().replace("-", "");
        log(initTraceId, "INFO", "Initializing UdsBridgeServer on AF_UNIX socket: " + socketPath);

        // Delete stale socket file if it exists
        try {
            Files.deleteIfExists(socketPath);
        } catch (IOException e) {
            log(initTraceId, "WARN", "Could not delete existing socket file: " + e.getMessage());
        }

        // Ensure parent directory exists
        if (socketPath.getParent() != null) {
            Files.createDirectories(socketPath.getParent());
        }

        UnixDomainSocketAddress address = UnixDomainSocketAddress.of(socketPath);
        serverChannel = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
        serverChannel.bind(address);

        // Shutdown hook to cleanly delete socket file
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            RUNNING.set(false);
            try {
                if (serverChannel != null && serverChannel.isOpen()) {
                    serverChannel.close();
                }
            } catch (Exception ignored) {}
            try {
                Files.deleteIfExists(socketPath);
            } catch (Exception ignored) {}
            log(initTraceId, "INFO", "UdsBridgeServer shutdown hook completed; socket unlinked.");
        }));

        log(initTraceId, "INFO", "UdsBridgeServer bound and listening on: " + socketPath);

        while (RUNNING.get()) {
            try {
                SocketChannel client = serverChannel.accept();
                if (client == null) continue;
                clientExecutor.submit(() -> handleClient(client));
            } catch (Exception e) {
                if (RUNNING.get()) {
                    log(initTraceId, "ERROR", "Accept loop exception: " + e.getMessage());
                }
            }
        }

        close();
    }

    public void close() {
        RUNNING.set(false);
        try {
            if (serverChannel != null && serverChannel.isOpen()) {
                serverChannel.close();
            }
        } catch (Exception ignored) {}
        try {
            Files.deleteIfExists(socketPath);
        } catch (Exception ignored) {}
        clientExecutor.shutdownNow();
    }

    private void handleClient(SocketChannel client) {
        String clientTraceId = UUID.randomUUID().toString().replace("-", "");
        try (client;
             InputStream rawIn = Channels.newInputStream(client);
             OutputStream rawOut = Channels.newOutputStream(client);
             BufferedInputStream in = new BufferedInputStream(rawIn);
             BufferedOutputStream out = new BufferedOutputStream(rawOut)) {

            while (RUNNING.get() && client.isOpen()) {
                in.mark(8);
                byte[] header = in.readNBytes(4);
                if (header.length < 4) {
                    // Client closed stream cleanly
                    break;
                }

                boolean isNdjson = (header[0] == '{' || Character.isWhitespace(header[0]));
                byte[] requestBytes;

                if (isNdjson) {
                    in.reset();
                    ByteArrayOutputStream lineBuf = new ByteArrayOutputStream();
                    int b;
                    while ((b = in.read()) != -1) {
                        if (b == '\n') break;
                        lineBuf.write(b);
                    }
                    requestBytes = lineBuf.toByteArray();
                    if (requestBytes.length == 0) continue;
                } else {
                    int frameLen = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN).getInt();
                    if (frameLen <= 0 || frameLen > MAX_FRAME_SIZE) {
                        throw new IOException("Invalid IPC frame length: " + frameLen);
                    }
                    requestBytes = in.readNBytes(frameLen);
                    if (requestBytes.length < frameLen) {
                        throw new EOFException("Premature EOF reading frame body");
                    }
                }

                JsonNode reqJson;
                try {
                    reqJson = MAPPER.readTree(requestBytes);
                } catch (Exception ex) {
                    log(clientTraceId, "ERROR", "Malformed JSON request: " + ex.getMessage());
                    sendErrorResponse(out, isNdjson, "Malformed JSON request", clientTraceId);
                    continue;
                }

                String traceId = reqJson.has("trace_id") ? reqJson.get("trace_id").asText() : clientTraceId;
                String action = extractAction(reqJson);
                log(traceId, "DEBUG", "Received command: " + action);

                ObjectNode responseNode;
                boolean isShutdown = false;

                try {
                    switch (action.toLowerCase()) {
                        case "ping" -> responseNode = handlePing(traceId);
                        case "retrieve" -> responseNode = handleRetrieve(reqJson, traceId);
                        case "retrieve_full" -> responseNode = handleRetrieveFull(reqJson, traceId);
                        case "bounds", "store_bounds" -> responseNode = handleBounds(reqJson, traceId);
                        case "shutdown" -> {
                            responseNode = handleShutdown(traceId);
                            isShutdown = true;
                        }
                        default -> {
                            responseNode = MAPPER.createObjectNode();
                            responseNode.put("status", "ERROR");
                            responseNode.put("error", "Unknown command: " + action);
                            responseNode.put("trace_id", traceId);
                        }
                    }
                } catch (Exception ex) {
                    log(traceId, "ERROR", "Command execution error: " + ex.getMessage());
                    responseNode = MAPPER.createObjectNode();
                    responseNode.put("status", "ERROR");
                    responseNode.put("error", ex.getMessage());
                    responseNode.put("error_message", ex.getMessage());
                    responseNode.put("trace_id", traceId);
                }

                byte[] respBytes = MAPPER.writeValueAsBytes(responseNode);
                if (isNdjson) {
                    out.write(respBytes);
                    out.write('\n');
                    out.flush();
                } else {
                    ByteBuffer lenBuf = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(respBytes.length);
                    out.write(lenBuf.array());
                    out.write(respBytes);
                    out.flush();
                }

                if (isShutdown) {
                    log(traceId, "INFO", "Shutdown requested; terminating daemon.");
                    new Thread(() -> {
                        try {
                            Thread.sleep(50);
                        } catch (InterruptedException ignored) {}
                        close();
                        System.exit(0);
                    }).start();
                    break;
                }
            }
        } catch (IOException e) {
            log(clientTraceId, "DEBUG", "Client disconnected: " + e.getMessage());
        }
    }

    private static String extractAction(JsonNode reqJson) {
        if (reqJson.has("action")) {
            return reqJson.get("action").asText();
        }
        if (reqJson.has("command")) {
            return reqJson.get("command").asText();
        }
        if (reqJson.has("ping")) {
            return "ping";
        }
        if (reqJson.has("retrieve")) {
            return "retrieve";
        }
        if (reqJson.has("retrieve_full")) {
            return "retrieve_full";
        }
        if (reqJson.has("bounds") || reqJson.has("store_bounds")) {
            return "bounds";
        }
        if (reqJson.has("shutdown")) {
            return "shutdown";
        }
        return "ping";
    }

    private static void sendErrorResponse(OutputStream out, boolean isNdjson, String message, String traceId) throws IOException {
        ObjectNode err = MAPPER.createObjectNode();
        err.put("status", "ERROR");
        err.put("error", message);
        err.put("trace_id", traceId);
        byte[] b = MAPPER.writeValueAsBytes(err);
        if (isNdjson) {
            out.write(b);
            out.write('\n');
        } else {
            ByteBuffer lenBuf = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(b.length);
            out.write(lenBuf.array());
            out.write(b);
        }
        out.flush();
    }

    private ObjectNode handlePing(String traceId) {
        ObjectNode res = MAPPER.createObjectNode();
        res.put("status", "OK");
        res.put("pong", true);
        res.put("trace_id", traceId);
        return res;
    }

    private ObjectNode handleShutdown(String traceId) {
        ObjectNode res = MAPPER.createObjectNode();
        res.put("status", "OK");
        res.put("shutting_down", true);
        res.put("trace_id", traceId);
        return res;
    }

    private ObjectNode handleBounds(JsonNode req, String traceId) throws IOException {
        String storePathStr = req.has("store_path") ? req.get("store_path").asText()
                : (req.has("store") ? req.get("store").asText() : null);

        if (storePathStr == null || storePathStr.isBlank()) {
            throw new IllegalArgumentException("store_path is required");
        }

        Path sPath = Path.of(storePathStr).toAbsolutePath().normalize();
        if (!Files.exists(sPath)) {
            throw new IOException("Store path does not exist: " + sPath);
        }

        ObjectNode res = MAPPER.createObjectNode();
        res.put("trace_id", traceId);

        // Check if sPath is a segmented store directory (or blocks.dat inside segmented store)
        Path rootDir = Files.isDirectory(sPath) ? sPath : sPath.getParent();
        if (rootDir != null && (Files.exists(rootDir.resolve("block-metadata.dat")) || Files.exists(rootDir.resolve("segments")))) {
            Path blocksDat = rootDir.resolve("blocks.dat");
            Path metaDat = rootDir.resolve("block-metadata.dat");
            Path envCfg = rootDir.resolve("envelope.cfg");
            if (!Files.exists(envCfg)) {
                envCfg = Path.of("fss-core/envelope.cfg").toAbsolutePath();
            }
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
                RetrievalService rService = new RetrievalService(cfg, bStore, mStore, sStore);
                StoreBounds bounds = rService.discoverBounds();
                res.put("status", bounds.status());
                if (bounds.min_ts_ns() != null) res.put("min_ts_ns", bounds.min_ts_ns());
                if (bounds.max_ts_ns() != null) res.put("max_ts_ns", bounds.max_ts_ns());
                res.put("segment_count", bounds.segment_count());
                res.put("block_count", bounds.block_count());
                ArrayNode winArr = res.putArray("segment_windows");
                for (StoreBounds.SegmentWindow sw : bounds.segment_windows()) {
                    ObjectNode w = winArr.addObject();
                    w.put("start_ts_ns", sw.start_ts_ns());
                    w.put("end_ts_ns", sw.end_ts_ns());
                    w.put("block_count", sw.block_count());
                }
                ArrayNode srcArr = res.putArray("known_sources");
                for (Long src : bounds.known_sources()) {
                    srcArr.add(src);
                }
                return res;
            } finally {
                mStore.close();
                bStore.close();
            }
        }

        // Single-file J1 .dat store
        ReentrantReadWriteLock rwLock = getLock(sPath);
        rwLock.readLock().lock();
        try {
            try (BlockStore store = openWithRetry(sPath, 3000)) {
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
                if (found && Long.compareUnsigned(minTs, maxTs) <= 0) {
                    long maxTsQuery = (maxTs == -1L) ? -1L : maxTs + 1L;
                    res.put("status", "OK");
                    res.put("min_ts_ns", Long.toUnsignedString(minTs));
                    res.put("max_ts_ns", Long.toUnsignedString(maxTsQuery));
                    res.put("segment_count", 1);
                    res.put("block_count", blocks.size());
                    ArrayNode winArr = res.putArray("segment_windows");
                    ObjectNode w = winArr.addObject();
                    w.put("start_ts_ns", Long.toUnsignedString(minTs));
                    w.put("end_ts_ns", Long.toUnsignedString(maxTsQuery));
                    w.put("block_count", nonEmpBlocks);
                    res.putArray("known_sources");
                } else {
                    res.put("status", "EMPTY");
                    res.put("segment_count", 0);
                    res.put("block_count", blocks.size());
                    res.putArray("segment_windows");
                    res.putArray("known_sources");
                }
                return res;
            }
        } finally {
            rwLock.readLock().unlock();
        }
    }

    private ObjectNode handleRetrieve(JsonNode req, String traceId) throws IOException {
        String storePathStr = req.has("store_path") ? req.get("store_path").asText()
                : (req.has("store") ? req.get("store").asText() : null);

        if (storePathStr == null || storePathStr.isBlank()) {
            throw new IllegalArgumentException("store_path is required");
        }

        Path sPath = Path.of(storePathStr).toAbsolutePath().normalize();
        if (!Files.exists(sPath)) {
            throw new IOException("Store file does not exist: " + sPath);
        }

        // Parse source_id
        boolean allSources = true;
        long sourceId = -1;
        if (req.has("source_id") && !req.get("source_id").isNull()) {
            JsonNode sNode = req.get("source_id");
            if (sNode.isTextual() && sNode.asText().equalsIgnoreCase("all")) {
                allSources = true;
            } else if (sNode.isNumber()) {
                long val = sNode.longValue();
                if (val != -1) {
                    allSources = false;
                    sourceId = val;
                }
            } else if (sNode.isTextual()) {
                allSources = false;
                sourceId = Long.parseUnsignedLong(sNode.asText());
            }
        } else if (req.has("source") && !req.get("source").isNull()) {
            JsonNode sNode = req.get("source");
            if (sNode.isTextual() && sNode.asText().equalsIgnoreCase("all")) {
                allSources = true;
            } else if (sNode.isNumber()) {
                long val = sNode.longValue();
                if (val != -1) {
                    allSources = false;
                    sourceId = val;
                }
            } else if (sNode.isTextual()) {
                allSources = false;
                sourceId = Long.parseUnsignedLong(sNode.asText());
            }
        }

        // Parse t_start_ns
        long tStart = 0L;
        if (req.has("t_start_ns")) {
            tStart = parseUnsignedLong(req.get("t_start_ns"));
        } else if (req.has("start_ns")) {
            tStart = parseUnsignedLong(req.get("start_ns"));
        } else if (req.has("start_time")) {
            tStart = parseUnsignedLong(req.get("start_time"));
        }

        // Parse t_end_ns
        long tEnd = -1L; // 0xFFFFFFFFFFFFFFFFL unsigned
        if (req.has("t_end_ns")) {
            tEnd = parseUnsignedLong(req.get("t_end_ns"));
        } else if (req.has("end_ns")) {
            tEnd = parseUnsignedLong(req.get("end_ns"));
        } else if (req.has("end_time")) {
            tEnd = parseUnsignedLong(req.get("end_time"));
        }

        // Parse max_events
        int maxEvents = 10000;
        if (req.has("max_events") && req.get("max_events").isInt()) {
            maxEvents = req.get("max_events").asInt();
        }

        // Parse event_type
        Integer eventType = null;
        if (req.has("event_type") && !req.get("event_type").isNull()) {
            eventType = req.get("event_type").asInt();
        }

        List<EventRecord> events = new ArrayList<>();
        boolean truncated = false;

        ReentrantReadWriteLock rwLock = getLock(sPath);
        rwLock.writeLock().lock();
        try {
            try (BlockStore store = openWithRetry(sPath, 3000)) {
                List<BlockInfo> blocks = store.blocks();
                long minTs = Long.MAX_VALUE;
                long maxTs = Long.MIN_VALUE;
                for (BlockInfo b : blocks) {
                    if (!b.empty()) {
                        if (Long.compareUnsigned(b.minTsNs(), minTs) < 0) minTs = b.minTsNs();
                        if (Long.compareUnsigned(b.maxTsNs(), maxTs) > 0) maxTs = b.maxTsNs();
                    }
                }

                if (minTs == Long.MAX_VALUE) {
                    // Store is empty
                    return buildResponse(events, false, traceId);
                }

                if (Long.compareUnsigned(tEnd, tStart) <= 0) {
                    return buildResponse(events, false, traceId);
                }

                // If tEnd - tStart exceeds 1 hour (3600s), query in consecutive 1-hour chunks
                long curStart = (Long.compareUnsigned(tStart, minTs) < 0) ? minTs : tStart;
                long boundEnd = (Long.compareUnsigned(tEnd, maxTs) > 0) ? maxTs + 1 : tEnd;

                while (Long.compareUnsigned(curStart, boundEnd) < 0 && events.size() < maxEvents) {
                    long curEnd;
                    if (Long.compareUnsigned(boundEnd - curStart, RetrieveRequest.MAX_WINDOW_NS) <= 0) {
                        curEnd = boundEnd;
                    } else {
                        curEnd = curStart + RetrieveRequest.MAX_WINDOW_NS;
                    }

                    int remainingMax = Math.min(maxEvents - events.size(), RetrieveRequest.MAX_EVENTS_LIMIT);
                    RetrieveRequest q = allSources
                            ? RetrieveRequest.forAllSources(curStart, curEnd, remainingMax)
                            : RetrieveRequest.forSource((int) sourceId, curStart, curEnd, remainingMax);

                    if (eventType != null) {
                        q = q.withEventType(eventType);
                    }

                    RetrieveResult res = store.retrieve(q);
                    events.addAll(res.events());

                    if (res.truncated() || events.size() >= maxEvents) {
                        truncated = true;
                        break;
                    }

                    curStart = curEnd;
                }
            }
        } finally {
            rwLock.writeLock().unlock();
        }

        return buildResponse(events, truncated, traceId);
    }

    private ObjectNode handleRetrieveFull(JsonNode req, String traceId) throws IOException {
        String storePathStr = req.has("store_path") ? req.get("store_path").asText()
                : (req.has("store") ? req.get("store").asText() : null);

        if (storePathStr == null || storePathStr.isBlank()) {
            throw new IllegalArgumentException("store_path is required");
        }

        Path sPath = Path.of(storePathStr).toAbsolutePath().normalize();
        if (!Files.exists(sPath)) {
            throw new IOException("Store file does not exist: " + sPath);
        }

        List<EventRecord> events = new ArrayList<>();

        ReentrantReadWriteLock rwLock = getLock(sPath);
        rwLock.writeLock().lock();
        try {
            // First run crash recovery by opening with retry
            try (BlockStore store = openWithRetry(sPath, 3000)) {
                // Now read all valid blocks from the store
                try (FileChannel fc = FileChannel.open(sPath, StandardOpenOption.READ)) {
                    long size = fc.size();
                    int blockCount = (int) (size / EnvelopeCodec.BLOCK_SIZE);
                    ByteBuffer buf = ByteBuffer.allocate(EnvelopeCodec.BLOCK_SIZE).order(ByteOrder.LITTLE_ENDIAN);

                    for (int i = 0; i < blockCount; i++) {
                        buf.clear();
                        fc.position((long) i * EnvelopeCodec.BLOCK_SIZE);
                        while (buf.hasRemaining()) {
                            if (fc.read(buf) < 0) break;
                        }
                        buf.flip();

                        int off = 0;
                        while (EnvelopeCodec.BLOCK_SIZE - off >= EnvelopeCodec.MIN_RECORD_SIZE) {
                            int recordLen = buf.getInt(off);
                            if (recordLen == 0) break;
                            EnvelopeCodec.Error err = EnvelopeCodec.checkRecord(buf, off, EnvelopeCodec.BLOCK_SIZE - off);
                            if (err != null) break;
                            int envelope = off + EnvelopeCodec.RECORD_HEADER_LEN;
                            events.add(EnvelopeCodec.decodeEnvelope(buf, envelope, recordLen));
                            off += EnvelopeCodec.RECORD_HEADER_LEN + recordLen;
                        }
                    }
                }
            }
        } finally {
            rwLock.writeLock().unlock();
        }

        events.sort((a, b) -> Long.compareUnsigned(a.timestampNs(), b.timestampNs()));
        return buildResponse(events, false, traceId);
    }

    private ObjectNode buildResponse(List<EventRecord> events, boolean truncated, String traceId) {
        ObjectNode res = MAPPER.createObjectNode();
        res.put("status", "OK");
        res.put("count", events.size());
        res.put("truncated", truncated);
        res.put("trace_id", traceId);

        ArrayNode eventsArray = MAPPER.createArrayNode();
        for (EventRecord ev : events) {
            ObjectNode evNode = MAPPER.createObjectNode();
            evNode.put("source_id", ev.sourceId());
            evNode.put("timestamp_ns", Long.toUnsignedString(ev.timestampNs()));
            evNode.put("event_type", ev.eventType());
            evNode.put("payload_len", ev.payload().length);
            evNode.put("payload_hex", HEX.formatHex(ev.payload()));
            String b64 = B64.encodeToString(ev.payload());
            evNode.put("payload_base64", b64);
            evNode.put("payload_b64", b64);
            eventsArray.add(evNode);
        }
        res.set("events", eventsArray);
        return res;
    }

    private static long parseUnsignedLong(JsonNode node) {
        if (node.isTextual()) {
            return Long.parseUnsignedLong(node.asText());
        }
        if (node.isNumber()) {
            return node.asLong();
        }
        return 0L;
    }

    public static void main(String[] args) {
        // Diagnostic logs must strictly go to System.err
        System.setOut(System.err);

        String socketPathStr = System.getenv("FSS_SOCKET_PATH");
        if (socketPathStr == null || socketPathStr.isBlank()) {
            socketPathStr = DEFAULT_SOCKET_PATH;
        }

        for (int i = 0; i < args.length; i++) {
            if ("--socket".equals(args[i]) && i + 1 < args.length) {
                socketPathStr = args[++i];
            }
        }

        Path socketPath = Path.of(socketPathStr).toAbsolutePath().normalize();
        UdsBridgeServer server = new UdsBridgeServer(socketPath);

        try {
            server.start();
        } catch (Exception e) {
            log("main", "FATAL", "Failed to start UdsBridgeServer: " + e.getMessage());
            e.printStackTrace(System.err);
            System.exit(1);
        }
    }
}