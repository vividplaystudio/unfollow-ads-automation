<?php
/**
 * RevenueCat webhook receiver.
 *
 * RC posts every subscription event (INITIAL_PURCHASE, RENEWAL, CANCELLATION,
 * etc.) to this endpoint. We append raw events to rc_events.jsonl so the
 * dashboard can bucket revenue by real transaction date instead of inferring.
 *
 * Setup:
 *   1. Store the shared secret in .rc_webhook_secret on the server (file mode 0600).
 *   2. In RevenueCat dashboard → Integrations → Webhooks, set:
 *      - URL: https://genivox.com/ads-upload/rc_webhook.php
 *      - Authorization header value: <same secret>
 *
 * Events end up in rc_events.jsonl, one JSON object per line.
 */

header("Content-Type: application/json");
header("X-Content-Type-Options: nosniff");

// ── Auth: check Authorization header matches the stored secret ──────────────
$secret_file = __DIR__ . '/.rc_webhook_secret';
if (!file_exists($secret_file)) {
    http_response_code(503);
    echo json_encode(['error' => 'Secret file not provisioned']);
    error_log('rc_webhook: .rc_webhook_secret missing');
    exit;
}
$expected = trim(@file_get_contents($secret_file));
if ($expected === '') {
    http_response_code(503);
    echo json_encode(['error' => 'Secret is empty']);
    exit;
}

$got = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
// RC sends the raw value, not "Bearer …". Accept both forms.
if (stripos($got, 'Bearer ') === 0) {
    $got = trim(substr($got, 7));
}
if (!hash_equals($expected, $got)) {
    http_response_code(401);
    echo json_encode(['error' => 'Unauthorized']);
    exit;
}

// ── Only POST ───────────────────────────────────────────────────────────────
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'Only POST allowed']);
    exit;
}

$raw = file_get_contents('php://input');
if (!$raw) {
    http_response_code(400);
    echo json_encode(['error' => 'Empty body']);
    exit;
}

$payload = json_decode($raw, true);
if (!is_array($payload) || !isset($payload['event'])) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid JSON / missing event']);
    exit;
}

$event = $payload['event'];

// Drop sandbox events — only production revenue counts.
$env = $event['environment'] ?? '';
if ($env !== '' && $env !== 'PRODUCTION') {
    echo json_encode(['ok' => true, 'skipped' => 'non-production']);
    exit;
}

// ── Append the event to the log (atomic append with exclusive lock) ─────────
$log_file = __DIR__ . '/rc_events.jsonl';

// Record the server receive time so we can verify freshness later.
$record = [
    'received_at' => date('c'),
    'event' => $event,
];

$line = json_encode($record, JSON_UNESCAPED_SLASHES) . "\n";

$fp = @fopen($log_file, 'ab');
if (!$fp) {
    http_response_code(500);
    echo json_encode(['error' => 'Cannot open log file']);
    error_log('rc_webhook: cannot open ' . $log_file);
    exit;
}
flock($fp, LOCK_EX);
fwrite($fp, $line);
fflush($fp);
flock($fp, LOCK_UN);
fclose($fp);

// Optional file-size safety: if the file grows huge, rotate.
// ~50 MB cap keeps us well under most shared-host disk quotas.
clearstatcache();
if (@filesize($log_file) > 50 * 1024 * 1024) {
    @rename($log_file, $log_file . '.' . date('Ymd-His'));
}

echo json_encode(['ok' => true]);
