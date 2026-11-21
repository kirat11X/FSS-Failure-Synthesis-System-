
package Retrieval;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

public final class RetrievalIpcServer {

    private static final int MAX_FRAME_SIZE = 64 * 1024 * 1024;

    private final RetrievalService retrievalService;
    private final ObjectMapper mapper;
    private final DataInputStream input;
    private final DataOutputStream output;

    public RetrievalIpcServer(RetrievalService retrievalService) {
        this(retrievalService, System.in, System.out);
    }

    public RetrievalIpcServer(RetrievalService retrievalService, java.io.InputStream in, java.io.OutputStream out) {
        this.retrievalService = retrievalService;
        this.mapper = new ObjectMapper();
        this.input = (in instanceof DataInputStream dis) ? dis : new DataInputStream(in);
        this.output = (out instanceof DataOutputStream dos) ? dos : new DataOutputStream(out);
    }

    public void run() throws IOException {

        while (true) {

            final int requestLength;

            try {
                requestLength = input.readInt();
            } catch (EOFException e) {
                // Python closed its input stream.
                return;
            }

            if (requestLength <= 0 || requestLength > MAX_FRAME_SIZE) {
                throw new IOException(
                        "Invalid IPC request length: " + requestLength);
            }

            byte[] requestBytes = new byte[requestLength];
            input.readFully(requestBytes);

            byte[] responseBytes;

            try {
                JsonNode requestJson = mapper.readTree(requestBytes);

                String action = "";
                if (requestJson.has("action")) {
                    action = requestJson.get("action").asText();
                } else if (requestJson.has("command")) {
                    action = requestJson.get("command").asText();
                } else if (requestJson.has("bounds")) {
                    action = "bounds";
                }

                if ("bounds".equalsIgnoreCase(action) || "store_bounds".equalsIgnoreCase(action)) {
                    StoreBounds bounds = retrievalService.discoverBounds();
                    responseBytes = mapper.writeValueAsBytes(bounds);
                } else {
                    RetrievalRequest request = RetrievalRequest.fromJson(requestJson);

                    RetrievalResponse response = RetrievalResponse.success(
                            retrievalService.retrieve(
                                    request.allSources(),
                                    request.agentId(),
                                    request.startTime(),
                                    request.endTime(),
                                    request.eventType()));

                    responseBytes = mapper.writeValueAsBytes(response);
                }

            } catch (Exception e) {

                System.err.println(
                        "Retrieval request failed: " + e.getMessage());

                RetrievalResponse response = RetrievalResponse.failure(e.getMessage());

                responseBytes = mapper.writeValueAsBytes(response);
            }

            // 4-byte big-endian length followed by JSON response.
            output.writeInt(responseBytes.length);
            output.write(responseBytes);
            output.flush();
        }
    }
}
