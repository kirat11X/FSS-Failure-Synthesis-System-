package Retrieval;

import java.util.List;

public record RetrievalResponse(
        String status,
        List<RetrievalRecord> records,
        String error,
        Integer totalMatches,
        Boolean truncated) {

    public static RetrievalResponse success(List<RetrievalRecord> records) {
        return new RetrievalResponse("OK", records, null, records.size(), false);
    }

    public static RetrievalResponse success(
            List<RetrievalRecord> records,
            int totalMatches,
            boolean truncated) {
        return new RetrievalResponse("OK", records, null, totalMatches, truncated);
    }

    public static RetrievalResponse failure(String error) {
        return new RetrievalResponse("ERROR", List.of(), error, 0, false);
    }
}
