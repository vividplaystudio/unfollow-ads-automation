<?php
/**
 * Receives scan health reports from the app (lib/services/ig/ig_health.dart).
 *
 * Public on purpose — the app can't keep a secret — so the folder's Basic
 * Auth is lifted for this one file (.github/workflows/fix-ig-health-htaccess.yml).
 * It accepts only small, well-formed, anonymous reports, throttles per IP,
 * stores nothing that identifies a person, and re-checks alerts at most once
 * every 5 minutes.
 */

header('Content-Type: application/json');
header('X-Content-Type-Options: nosniff');

require __DIR__ . '/ig_health_lib.php';

const IGH_APP_KEY = 'ufp-ig-health-1';
const IGH_MAX_BODY = 65536;

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'POST only']);
    exit;
}

$raw = file_get_contents('php://input', false, null, 0, IGH_MAX_BODY + 1);
if ($raw === false || $raw === '') {
    http_response_code(400);
    echo json_encode(['error' => 'Empty body']);
    exit;
}
if (strlen($raw) > IGH_MAX_BODY) {
    http_response_code(413);
    echo json_encode(['error' => 'Too large']);
    exit;
}

$report = json_decode($raw, true);
if (!is_array($report) || !isset($report['app_key']) || $report['app_key'] !== IGH_APP_KEY
    || !isset($report['events']) || !is_array($report['events'])) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid report']);
    exit;
}

if (!igh_rate_ok(isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '')) {
    http_response_code(429);
    echo json_encode(['error' => 'Slow down']);
    exit;
}

if (!igh_append(igh_sanitize($report))) {
    http_response_code(500);
    echo json_encode(['error' => 'Cannot store report']);
    exit;
}

igh_maybe_evaluate_alerts();
echo json_encode(['ok' => true]);
