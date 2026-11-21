package Retrieval;

import java.util.List;

public record StoreBounds(
        String status,
        String min_ts_ns,
        String max_ts_ns,
        int segment_count,
        int block_count,
        List<SegmentWindow> segment_windows,
        List<Long> known_sources,
        String error) {

    public record SegmentWindow(
            String start_ts_ns,
            String end_ts_ns,
            int block_count) {
    }

    public static StoreBounds ok(
            long minTs,
            long maxTs,
            int segmentCount,
            int blockCount,
            List<SegmentWindow> windows,
            List<Long> sources) {
        return new StoreBounds(
                "OK",
                Long.toUnsignedString(minTs),
                Long.toUnsignedString(maxTs),
                segmentCount,
                blockCount,
                windows,
                sources,
                null);
    }

    public static StoreBounds empty() {
        return new StoreBounds("EMPTY", null, null, 0, 0, List.of(), List.of(), null);
    }

    public static StoreBounds failure(String error) {
        return new StoreBounds("ERROR", null, null, 0, 0, List.of(), List.of(), error);
    }
}
