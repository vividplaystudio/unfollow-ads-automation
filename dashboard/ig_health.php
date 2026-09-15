<?php
/**
 * Data for the Instagram health panel (ig-health.html).
 *
 * Stays behind the folder's HTTP Basic Auth — only ig_ingest.php is exempt.
 *   ?hours=24            summary JSON (1–72)
 *   ?action=test_alert   send a test message to the configured alert channels
 */

header('Content-Type: application/json');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

require __DIR__ . '/ig_health_lib.php';

$action = isset($_GET['action']) ? $_GET['action'] : 'summary';

if ($action === 'test_alert') {
    $sent = igh_notify("Unfollow Tracker — test message from the Instagram health panel.\nIf you can read this, alerts are working.\n\n" . IGH_PANEL_URL);
    echo json_encode(['sent' => $sent, 'channels' => igh_alert_channels()]);
    exit;
}

$hours = isset($_GET['hours']) ? (int) $_GET['hours'] : 24;
echo json_encode(igh_summary(max(1, min(72, $hours))));
