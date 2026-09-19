<?php
/**
 * Checks the username screen's profile lookup FROM THIS SERVER.
 *
 * Why here and not in CI: Instagram throttles datacenter addresses hard, so a
 * GitHub runner sees 429 on every way and learns nothing. This server's address
 * behaves much more like a real visitor, so the answer is worth acting on.
 *
 * It reads the same recipe the app uses (Remote Config `ig_playbook`, falling
 * back to the copy beside this file), tries every way, and messages Telegram
 * only when a way fails for a reason that is NOT rate limiting.
 *
 * Call it with the same shared secret as the webhook:
 *   curl -H "Authorization: <secret>" https://<host>/ig_prelogin_check.php
 *
 * Prints no usernames, ids or tokens — only the public account it looks up.
 */

require_once __DIR__ . '/ig_health_lib.php';

header('Content-Type: application/json; charset=UTF-8');

// ── Auth: same shared secret as rc_webhook ──────────────────────────────────
$secret_file = __DIR__ . '/.rc_webhook_secret';
$expected = is_readable($secret_file) ? trim(@file_get_contents($secret_file)) : '';
$got = '';
foreach (['HTTP_AUTHORIZATION', 'REDIRECT_HTTP_AUTHORIZATION'] as $k) {
    if (!empty($_SERVER[$k])) { $got = trim($_SERVER[$k]); break; }
}
if ($expected === '' || !hash_equals($expected, $got)) {
    http_response_code(403);
    echo json_encode(['error' => 'forbidden']);
    exit;
}

const IGPC_ACCOUNT = 'instagram';   // a public account, never a user's
const IGPC_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
              . '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';

function igpc_get($url, $headers)
{
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_TIMEOUT        => 15,
        CURLOPT_USERAGENT      => IGPC_UA,
        CURLOPT_HTTPHEADER     => $headers,
    ]);
    $body = curl_exec($ch);
    $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err  = curl_error($ch);
    curl_close($ch);
    return [$code, $body === false ? '' : $body, $err];
}

/**
 * Whether a 200 is Instagram's login wall rather than a profile.
 *
 * It is served to addresses Instagram does not trust — this server among
 * them — and it is why the check reported the username screen DOWN on
 * 2026-09-19 while every phone was working normally.
 */
function igpc_is_login_wall($body)
{
    if ($body === '' || $body === null) return false;
    if (stripos($body, '"require_login"') !== false) return true;
    // A profile page always carries og:image. Without it, a page that talks
    // about logging in is the wall rather than a broken profile.
    if (stripos($body, 'og:image') !== false) return false;
    foreach (['accounts/login', 'loginForm', 'Log in to Instagram', 'LoginAndSignupPage'] as $sign) {
        if (stripos($body, $sign) !== false) return true;
    }
    return false;
}

/** up | down | unknown, with a line per way. */
function igpc_check($username)
{
    $ways = [];
    $ok = false;
    $realFailure = false;

    $attempts = [
        ['mobile_api', 'https://i.instagram.com/api/v1/users/web_profile_info/?username=' . rawurlencode($username),
            ['X-IG-App-ID: 936619743392459', 'Accept: */*'], 'json'],
        ['web_api', 'https://www.instagram.com/api/v1/users/web_profile_info/?username=' . rawurlencode($username),
            ['X-IG-App-ID: 936619743392459', 'Accept: */*'], 'json'],
        ['html_og', 'https://www.instagram.com/' . rawurlencode($username) . '/',
            ['Accept: text/html'], 'html'],
    ];

    foreach ($attempts as [$name, $url, $headers, $kind]) {
        [$code, $body, $err] = igpc_get($url, $headers);
        if ($err !== '') {
            $ways[] = "FAIL $name: network ($err)";
            $realFailure = true;
            continue;
        }
        if ($code === 401 || $code === 429) {
            $ways[] = "SKIP $name: $code blocked from this server";  // tells us nothing
            continue;
        }
        // Instagram answers a blocked address with a login wall as well as a
        // 401 — a real page, 200, just not a profile. From a datacenter it
        // says nothing about what a phone sees, so it is a skip like the 401
        // and must never raise the alarm on its own.
        if (igpc_is_login_wall($body)) {
            $ways[] = "SKIP $name: login wall (this server is blocked)";
            continue;
        }
        if ($code !== 200) {
            $ways[] = "FAIL $name: HTTP $code";
            $realFailure = true;
            continue;
        }
        $found = false;
        if ($kind === 'json') {
            $data = json_decode($body, true);
            $found = isset($data['data']['user']['profile_pic_url'])
                  || isset($data['data']['user']['profile_pic_url_hd']);
        } else {
            $found = (bool) preg_match('/<meta property="og:image" content="([^"]+)"/', $body);
        }
        if ($found) {
            $ways[] = "OK   $name: profile found";
            $ok = true;
        } else {
            $ways[] = "FAIL $name: 200 but the profile fields were missing";
            $realFailure = true;
        }
    }

    if ($ok)           return ['up', $ways];
    if ($realFailure)  return ['down', $ways];
    return ['unknown', $ways];                       // every way throttled
}

[$verdict, $ways] = igpc_check(IGPC_ACCOUNT);

$alerted = false;
if ($verdict === 'down') {
    $text = "Unfollow Tracker — the username screen is DOWN\n\n"
          . "Every way of looking up a profile failed, and not because of rate limiting. "
          . "New users cannot get past the first screen, so the ads are spending for nothing.\n\n"
          . implode("\n", $ways) . "\n\n"
          . "The fix is usually a new recipe in Firebase Remote Config (key: ig_playbook) — "
          . "no App Store update needed.";
    $alerted = igh_notify($text);
}

echo json_encode([
    'verdict' => $verdict,
    'ways'    => $ways,
    'alerted' => $alerted,
    'checked' => gmdate('c'),
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
