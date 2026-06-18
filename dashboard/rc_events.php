<?php
/**
 * Read the captured webhook events for the dashboard refresher.
 *
 * Authenticated with the same shared secret as rc_webhook.php.
 * Returns JSON events (one object per line of rc_events.jsonl).
 *
 * Query params:
 *   since_ms  (optional) — only return events whose purchased_at_ms >= this
 *   limit     (optional) — cap on events returned (default 50000)
 */

header("Content-Type: application/json");
header("X-Content-Type-Options: nosniff");

// ── Auth ────────────────────────────────────────────────────────────────────
// We accept the bearer token via EITHER the Authorization header OR a ?token=
// query string. The folder is also protected by Apache basic auth, and HTTP
// allows only one Authorization header per request, so callers that need to
// authenticate at both layers (basic auth + this bearer) pass the bearer in
// the URL while Authorization carries the basic credentials.
$secret_file = __DIR__ . '/.rc_webhook_secret';
if (!file_exists($secret_file)) {
    http_response_code(503);
    echo json_encode(['error' => 'Secret not provisioned']);
    exit;
}
$expected = trim(@file_get_contents($secret_file));

// 1. Try Authorization header (Bearer or raw)
$got = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
if (stripos($got, 'Bearer ') === 0) {
    $got = trim(substr($got, 7));
} elseif (stripos($got, 'Basic ') === 0) {
    // The Authorization header is basic — caller can't put the bearer here.
    // Fall through to URL-param check below.
    $got = '';
}

// 2. Fallback to ?token= query string (length-bounded to avoid log bloat).
if ($got === '' && isset($_GET['token'])) {
    $tok = (string) $_GET['token'];
    if (strlen($tok) > 0 && strlen($tok) < 512) {
        $got = $tok;
    }
}

if ($expected === '' || $got === '' || !hash_equals($expected, $got)) {
    http_response_code(401);
    echo json_encode(['error' => 'Unauthorized']);
    exit;
}

// ── Read the log, stream line-by-line to avoid loading everything ───────────
$log_file = __DIR__ . '/rc_events.jsonl';
if (!file_exists($log_file)) {
    echo json_encode(['events' => [], 'count' => 0]);
    exit;
}

$since_ms = isset($_GET['since_ms']) ? (int) $_GET['since_ms'] : 0;
$limit = isset($_GET['limit']) ? max(1, (int) $_GET['limit']) : 50000;

$out = [];
$skipped = 0;
$fp = @fopen($log_file, 'rb');
if (!$fp) {
    http_response_code(500);
    echo json_encode(['error' => 'Cannot open log file']);
    exit;
}
while (($line = fgets($fp)) !== false) {
    $line = trim($line);
    if ($line === '') continue;
    $rec = json_decode($line, true);
    if (!is_array($rec)) continue;
    $ev = $rec['event'] ?? null;
    if (!is_array($ev)) continue;

    $ts = 0;
    foreach (['purchased_at_ms', 'event_timestamp_ms'] as $k) {
        if (isset($ev[$k])) {
            $ts = (int) $ev[$k];
            break;
        }
    }

    if ($since_ms > 0 && $ts > 0 && $ts < $since_ms) {
        $skipped++;
        continue;
    }

    $out[] = $rec;
    if (count($out) >= $limit) break;
}
fclose($fp);

echo json_encode([
    'events' => $out,
    'count' => count($out),
    'skipped_before_since' => $skipped,
]);
