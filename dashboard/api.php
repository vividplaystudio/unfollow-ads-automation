<?php
/**
 * Apple Search Ads API proxy.
 *
 * Dashboard calls this endpoint to control keywords/ad groups/campaigns.
 * Token is stored in .token file (refreshed hourly by GitHub Actions).
 */

header("Content-Type: application/json");
header("X-Content-Type-Options: nosniff");

// Check origin/referer — only allow same-domain requests
$referer = $_SERVER['HTTP_REFERER'] ?? '';
$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
$allowed_hosts = ['genivox.com', 'www.genivox.com'];
$ok = false;
foreach ($allowed_hosts as $h) {
    if (strpos($referer, "://$h/") !== false || $origin === "https://$h") {
        $ok = true;
        break;
    }
}
if (!$ok) {
    http_response_code(403);
    echo json_encode(['error' => 'Forbidden — invalid origin']);
    exit;
}

// Read access token
$token_file = __DIR__ . '/.token';
if (!file_exists($token_file)) {
    http_response_code(503);
    echo json_encode(['error' => 'Token file not found. Wait for next refresh.']);
    exit;
}
$token = trim(@file_get_contents($token_file));
if (!$token) {
    http_response_code(503);
    echo json_encode(['error' => 'Token is empty. Wait for next refresh.']);
    exit;
}

// Only accept POST with JSON body
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'Only POST allowed']);
    exit;
}

$raw = file_get_contents('php://input');
$body = json_decode($raw, true);
if (!is_array($body)) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid JSON body']);
    exit;
}

$method = strtoupper($body['method'] ?? 'GET');
$path   = $body['path']   ?? '';
$data   = $body['data']   ?? null;
$org_id = '8868820';

// Safelist of allowed paths (regex patterns)
$allowed_patterns = [
    '#^/campaigns/\d+/adgroups/\d+/targetingkeywords/\d+$#',      // PUT keyword
    '#^/campaigns/\d+/adgroups/\d+/targetingkeywords/bulk$#',     // PATCH bulk keywords
    '#^/campaigns/\d+/adgroups/\d+$#',                            // PUT ad group
    '#^/campaigns/\d+$#',                                         // PUT campaign
];

$path_ok = false;
foreach ($allowed_patterns as $p) {
    if (preg_match($p, $path)) {
        $path_ok = true;
        break;
    }
}
if (!$path_ok) {
    http_response_code(400);
    echo json_encode(['error' => "Path not allowed: $path"]);
    exit;
}

// Only allow write methods
if (!in_array($method, ['PUT', 'PATCH', 'POST'])) {
    http_response_code(400);
    echo json_encode(['error' => "Method not allowed: $method"]);
    exit;
}

// Forward to Apple Search Ads
$url = "https://api.searchads.apple.com/api/v5" . $path;
$ch = curl_init($url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
curl_setopt($ch, CURLOPT_TIMEOUT, 30);
curl_setopt($ch, CURLOPT_HTTPHEADER, [
    "Authorization: Bearer $token",
    "X-AP-Context: orgId=$org_id",
    "Content-Type: application/json",
]);
if ($data !== null) {
    curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
}

$response = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err = curl_error($ch);
curl_close($ch);

if ($response === false) {
    http_response_code(500);
    echo json_encode(['error' => "ASA request failed: $err"]);
    exit;
}

http_response_code($code);
echo $response;
