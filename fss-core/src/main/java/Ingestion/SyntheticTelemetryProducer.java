
package Ingestion;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.time.Instant;
import java.util.concurrent.TimeUnit;

import Storage.EnvelopeConfig;

public final class SyntheticTelemetryProducer implements Runnable {

    private static final int AGENTS = 3;

    private static final int FORMATION_ID = 7;
    private static final int LEADER_ID = 1;

    // Incident timeline
    private static final long BASELINE_END = 5;
    private static final long MANEUVER_END = 8;
    private static final long FORMATION_LOST_END = 12;
    private static final long REJOINING_END = 17;

    // Formation states
    private static final byte IN_FORMATION = 1;
    private static final byte FORMATION_LOST = 2;
    private static final byte REJOINING = 3;

    // Navigation states
    private static final byte NORMAL = 0;
    private static final byte EMERGENCY_MANEUVER = 2;
    private static final byte RETURNING_TO_FORMATION = 3;

    private final TelemetryIngestor ingestor;
    private final EnvelopeConfig envelope;

    private volatile boolean running = true;
    private long sequence = 0;

    public SyntheticTelemetryProducer(
            TelemetryIngestor ingestor,
            EnvelopeConfig envelope) {

        this.ingestor = ingestor;
        this.envelope = envelope;
    }

    @Override
    public void run() {

        System.err.println("[Producer] Synthetic telemetry started");

        while (running) {

            sequence++;

            // One timestamp per simulation cycle.
            Instant now = Instant.now();

            long timestamp = TimeUnit.SECONDS.toNanos(now.getEpochSecond())
                    + now.getNano();

            for (int agent = 1; agent <= AGENTS; agent++) {

                boolean affectedAgent = agent == 2;

                byte formationState = IN_FORMATION;
                byte navigationState = NORMAL;
                byte avoidanceActive = 0;

                float minimumSeparation = 8.0f;
                float headingChange = 0.0f;

                if (affectedAgent) {

                    if (sequence <= BASELINE_END) {

                        // Normal operation.

                    } else if (sequence <= MANEUVER_END) {

                        // Agent 2 detects a close-proximity condition
                        // and performs an avoidance maneuver.
                        formationState = IN_FORMATION;
                        navigationState = EMERGENCY_MANEUVER;
                        avoidanceActive = 1;

                        minimumSeparation = 3.2f;
                        headingChange = -35.0f;

                    } else if (sequence <= FORMATION_LOST_END) {

                        // The maneuver is followed by reported
                        // formation loss.
                        formationState = FORMATION_LOST;
                        navigationState = EMERGENCY_MANEUVER;
                        avoidanceActive = 1;

                        minimumSeparation = 3.2f;
                        headingChange = -35.0f;

                    } else if (sequence <= REJOINING_END) {

                        // Agent 2 attempts to return to formation.
                        formationState = REJOINING;
                        navigationState = RETURNING_TO_FORMATION;
                        avoidanceActive = 0;

                        minimumSeparation = 5.0f;
                        headingChange = -8.0f;

                    } else {

                        // Recovery: Agent 2 reports normal operation.
                        formationState = IN_FORMATION;
                        navigationState = NORMAL;
                        avoidanceActive = 0;

                        minimumSeparation = 8.0f;
                        headingChange = 0.0f;
                    }
                }

                // Event 1: Formation status (4-byte payload)
                byte[] formationPayload = ByteBuffer.allocate(4)
                        .order(ByteOrder.LITTLE_ENDIAN)
                        .put(formationState)
                        .put((byte) FORMATION_ID)
                        .putShort((short) LEADER_ID)
                        .array();

                send(agent, timestamp, 1, formationPayload);

                // Event 2: Navigation and avoidance (12-byte payload)
                byte[] navigationPayload = ByteBuffer.allocate(12)
                        .order(ByteOrder.LITTLE_ENDIAN)
                        .put(navigationState)
                        .put(avoidanceActive)
                        .putShort((short) 0)
                        .putFloat(minimumSeparation)
                        .putFloat(headingChange)
                        .array();

                send(agent, timestamp, 2, navigationPayload);
            }

            try {
                Thread.sleep(1000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
        }

        System.err.println("[Producer] Stopped at sequence " + sequence);
    }

    private void send(
            long agentId,
            long timestamp,
            long eventType,
            byte[] payload) {

        byte[] message = createMessage(
                agentId, timestamp, eventType, payload);

        IngestionResult result = ingestor.ingest(message);

        System.err.println(
                "[Producer] " + result
                        + " | sequence=" + sequence
                        + " | agent=" + agentId
                        + " | event=" + eventType
                        + " | timestamp=" + timestamp
                        + " | payloadBytes=" + payload.length);
    }

    private byte[] createMessage(
            long agentId,
            long timestamp,
            long eventType,
            byte[] payloadBytes) {

        int payloadStart = envelope.field("Payload").start();

        byte[] message = new byte[payloadStart + payloadBytes.length];

        ByteBuffer buffer = ByteBuffer.wrap(message)
                .order(envelope.byteOrder());

        EnvelopeConfig.Field agent = envelope.field("Agent ID");

        if (agent.type() == EnvelopeConfig.Type.UINT32) {
            buffer.putInt(agent.start(), (int) agentId);
        } else if (agent.type() == EnvelopeConfig.Type.UINT64) {
            buffer.putLong(agent.start(), agentId);
        } else {
            throw new IllegalArgumentException(
                    "Unsupported Agent ID type: " + agent.type());
        }

        buffer.putLong(
                envelope.field("Timestamp").start(), timestamp);

        buffer.putInt(
                envelope.field("Payload Length").start(),
                payloadBytes.length);

        buffer.putInt(
                envelope.field("Event Type").start(),
                (int) eventType);

        System.arraycopy(
                payloadBytes, 0, message, payloadStart, payloadBytes.length);

        return message;
    }

    public void stop() {
        running = false;
    }
}
