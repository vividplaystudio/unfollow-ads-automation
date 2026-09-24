<?php
/**
 * Storage, aggregation and alerting for the Instagram health panel.
 *
 * Included by ig_ingest.php (writes reports) and ig_health.php (reads them).
 * Reports are anonymous operational facts sent by the app after each scan
 * (lib/services/ig/ig_health.dart): operation, strategy, outcome, HTTP
 * status, counts, timings, app/OS version, locale. No IP address is stored.
 *
 * Data lives in ig_health_data/YYYYMMDD.jsonl, one report per line, kept for
 * IGH_KEEP_DAYS. Written for PHP 7.x and 8.x.
 */

define('IGH_DIR', __DIR__ . '/ig_health_data');
define('IGH_KEEP_DAYS', 30);
define('IGH_SUCCESS', ['ok', 'empty', 'notFound']);

// Alerting: a 30-minute window compared with the 24 hours before it.
// Two hours, not thirty minutes: a quiet night hour held 23 scans, enough to
// call an operation broken and clear it again twenty minutes later. The panel
// still records everything; these numbers only decide what is worth a message.
define('IGH_ALERT_WINDOW', 7200);
define('IGH_ALERT_MIN_SAMPLE', 100);
define('IGH_BASELINE_MIN_SAMPLE', 200);

// One route failing while a backup carries the operation is the design
// working, not news — every orange alert so far was that. They stay on the
// panel, where they explain a red one; they no longer reach anyone's phone.
define('IGH_STRATEGY_ALERTS', false);

// One message each morning with the numbers that matter, so nobody has to open
// the panel to know the day went fine -- and nobody finds out from a customer's
// screenshot. Local time, because it is read over breakfast.
define('IGH_DIGEST_TZ', 'Asia/Bangkok');
define('IGH_DIGEST_HOUR', 9);
define('IGH_EVAL_EVERY', 300);
define('IGH_RENOTIFY_EVERY', 6 * 3600);
define('IGH_PANEL_URL', 'https://genivox.com/ads-upload/ig-health.html');
// Telegram already delivers every alert; the e-mail copy only repeated it.
// Set to true to get both again.
define('IGH_EMAIL_ALERTS', false);

function igh_dir()
{
    if (!is_dir(IGH_DIR)) {
        @mkdir(IGH_DIR, 0750, true);
    }
    $htaccess = IGH_DIR . '/.htaccess';
    if (!file_exists($htaccess)) {
        @file_put_contents($htaccess,
            "<IfModule mod_authz_core.c>\n    Require all denied\n</IfModule>\n" .
            "<IfModule !mod_authz_core.c>\n    Order allow,deny\n    Deny from all\n</IfModule>\n");
    }
    return IGH_DIR;
}

function igh_str($value, $max)
{
    if (!is_scalar($value)) {
        return '';
    }
    $clean = preg_replace('/[^A-Za-z0-9_ .:\-\/+|()]/', '', (string) $value);
    return substr($clean, 0, $max);
}

function igh_int($value, $min, $max)
{
    if (!is_numeric($value)) {
        return $min;
    }
    return (int) max($min, min($max, (int) $value));
}

// No array type on $map: callers pass not-yet-created entries by reference
// (e.g. $win[$op]), which arrive as null.
function igh_inc(&$map, $key, $by = 1)
{
    if (!is_array($map)) {
        $map = [];
    }
    $map[$key] = (isset($map[$key]) ? $map[$key] : 0) + $by;
}

/** Keep only known fields, typed and length-capped. */
function igh_sanitize(array $in)
{
    $events = [];
    foreach (array_slice($in['events'], 0, 200) as $e) {
        if (!is_array($e)) {
            continue;
        }
        $events[] = [
            'op' => igh_str(isset($e['op']) ? $e['op'] : '', 40),
            's'  => igh_str(isset($e['strategy']) ? $e['strategy'] : '', 64),
            'o'  => igh_str(isset($e['outcome']) ? $e['outcome'] : '', 20),
            'st' => igh_int(isset($e['status']) ? $e['status'] : 0, 0, 999),
            'n'  => igh_int(isset($e['count']) ? $e['count'] : 0, 0, 100000),
            'i'  => igh_int(isset($e['items']) ? $e['items'] : 0, 0, 10000000),
            'ms' => igh_int(isset($e['ms']) ? $e['ms'] : 0, 0, 100000000),
            'r'  => igh_int(isset($e['retries']) ? $e['retries'] : 0, 0, 100000),
        ];
    }

    $summary = [];
    if (isset($in['summary']) && is_array($in['summary'])) {
        foreach (array_slice($in['summary'], 0, 20, true) as $k => $v) {
            $key = igh_str($k, 32);
            if ($key === '') {
                continue;
            }
            if (is_bool($v)) {
                $summary[$key] = $v;
            } elseif (is_int($v) || is_float($v)) {
                $summary[$key] = igh_int($v, 0, 100000000);
            } else {
                $summary[$key] = igh_str($v, 32);
            }
        }
    }

    // Use the scan's own start time so reports queued offline land in the
    // hour they happened, not the hour they arrived — and never trigger a
    // "failing right now" alert after an outage.
    $now = time();
    $started = isset($in['started_at']) ? strtotime((string) $in['started_at']) : false;
    $t = ($started !== false && $started <= $now + 300 && $started >= $now - 7 * 86400) ? $started : $now;

    return [
        't'     => $t,
        'ctx'   => igh_str(isset($in['context']) ? $in['context'] : '', 16),
        'scan'  => igh_str(isset($in['scan_id']) ? $in['scan_id'] : '', 32),
        'app'   => igh_str(isset($in['app_version']) ? $in['app_version'] : '', 20),
        'build' => igh_str(isset($in['build']) ? $in['build'] : '', 12),
        'pf'    => igh_str(isset($in['platform']) ? $in['platform'] : '', 12),
        'os'    => igh_str(isset($in['os_version']) ? $in['os_version'] : '', 16),
        'cc'    => igh_str(isset($in['country']) ? $in['country'] : '', 4),
        'lang'  => igh_str(isset($in['lang']) ? $in['lang'] : '', 8),
        'pb'    => igh_str(isset($in['playbook']) ? $in['playbook'] : '', 48),
        'dur'   => igh_int(isset($in['duration_ms']) ? $in['duration_ms'] : 0, 0, 100000000),
        'ev'    => $events,
        'sum'   => $summary,
    ];
}

function igh_append(array $record)
{
    $file = igh_dir() . '/' . gmdate('Ymd', $record['t']) . '.jsonl';
    $fp = @fopen($file, 'ab');
    if (!$fp) {
        return false;
    }
    flock($fp, LOCK_EX);
    fwrite($fp, json_encode($record, JSON_UNESCAPED_SLASHES) . "\n");
    fflush($fp);
    flock($fp, LOCK_UN);
    fclose($fp);

    if (mt_rand(1, 500) === 1) {
        $cutoff = time() - IGH_KEEP_DAYS * 86400;
        foreach (glob(IGH_DIR . '/*.jsonl') ?: [] as $old) {
            if (@filemtime($old) < $cutoff) {
                @unlink($old);
            }
        }
    }
    return true;
}

/** Simple per-IP throttle (hashed, rotated daily, never written to the reports). */
function igh_rate_ok($ip, $limitPerMinute = 60)
{
    $bucket = igh_dir() . '/.rate-' . gmdate('YmdHi') . '.json';
    $key = substr(hash('sha256', $ip . gmdate('Ymd')), 0, 16);
    $fp = @fopen($bucket, 'c+');
    if (!$fp) {
        return true;
    }
    flock($fp, LOCK_EX);
    $counts = json_decode(stream_get_contents($fp) ?: '{}', true);
    if (!is_array($counts)) {
        $counts = [];
    }
    igh_inc($counts, $key);
    ftruncate($fp, 0);
    rewind($fp);
    fwrite($fp, json_encode($counts));
    flock($fp, LOCK_UN);
    fclose($fp);

    if (mt_rand(1, 50) === 1) {
        foreach (glob(IGH_DIR . '/.rate-*.json') ?: [] as $old) {
            if (@filemtime($old) < time() - 600) {
                @unlink($old);
            }
        }
    }
    return $counts[$key] <= $limitPerMinute;
}

function igh_load($since)
{
    $records = [];
    $day = strtotime(gmdate('Y-m-d', $since) . ' 00:00:00 UTC');
    for ($d = $day; $d <= time(); $d += 86400) {
        $file = IGH_DIR . '/' . gmdate('Ymd', $d) . '.jsonl';
        if (!is_readable($file)) {
            continue;
        }
        $fh = fopen($file, 'rb');
        while (($line = fgets($fh)) !== false) {
            $r = json_decode($line, true);
            if (is_array($r) && isset($r['t']) && $r['t'] >= $since && isset($r['ev']) && is_array($r['ev'])) {
                $records[] = $r;
            }
        }
        fclose($fh);
    }
    return $records;
}

/** op => true if any strategy succeeded in this report, false if all tried and failed. */
function igh_report_ops(array $record)
{
    $ops = [];
    foreach ($record['ev'] as $e) {
        if ($e['o'] === 'skipped') {
            continue;
        }
        if (!isset($ops[$e['op']])) {
            $ops[$e['op']] = false;
        }
        if (in_array($e['o'], IGH_SUCCESS, true)) {
            $ops[$e['op']] = true;
        }
    }
    return $ops;
}

function igh_summary($hours)
{
    $now = time();
    $records = igh_load($now - $hours * 3600);
    $windows = array_values(array_unique([1, 6, $hours]));

    $ops = [];
    $timeline = [];
    $strategies = [];
    $scans = ['total' => 0, 'outcome' => [], 'engagement' => [], 'username_missing' => 0, 'profile_pic_missing' => 0];
    $prelogin = ['total' => 0, 'outcome' => []];
    $versions = [];
    $playbooks = [];
    $lastReport = 0;

    foreach ($records as $r) {
        $lastReport = max($lastReport, $r['t']);
        $reportOps = igh_report_ops($r);
        $age = $now - $r['t'];

        foreach ($windows as $w) {
            if ($age > $w * 3600) {
                continue;
            }
            foreach ($reportOps as $op => $ok) {
                if (!isset($ops[$op][$w])) {
                    $ops[$op][$w] = ['n' => 0, 'ok' => 0];
                }
                $ops[$op][$w]['n']++;
                $ops[$op][$w]['ok'] += $ok ? 1 : 0;
            }
        }

        $hour = (int) floor($age / 3600);
        if ($hour < min($hours, 48)) {
            foreach ($reportOps as $op => $ok) {
                if (!isset($timeline[$op][$hour])) {
                    $timeline[$op][$hour] = ['n' => 0, 'ok' => 0];
                }
                $timeline[$op][$hour]['n']++;
                $timeline[$op][$hour]['ok'] += $ok ? 1 : 0;
            }
        }

        foreach ($r['ev'] as $e) {
            $k = $e['op'] . '|' . $e['s'];
            if (!isset($strategies[$k])) {
                $strategies[$k] = ['op' => $e['op'], 'strategy' => $e['s'], 'n' => 0, 'ms' => 0, 'items' => 0, 'outcomes' => [], 'statuses' => []];
            }
            $strategies[$k]['n'] += $e['n'];
            $strategies[$k]['ms'] += $e['ms'];
            $strategies[$k]['items'] += $e['i'];
            igh_inc($strategies[$k]['outcomes'], $e['o'], $e['n']);
            if ($e['st'] > 0 && !in_array($e['o'], IGH_SUCCESS, true)) {
                igh_inc($strategies[$k]['statuses'], (string) $e['st'], $e['n']);
            }
        }

        $sum = isset($r['sum']) && is_array($r['sum']) ? $r['sum'] : [];
        if ($r['ctx'] === 'prelogin') {
            $prelogin['total']++;
            igh_inc($prelogin['outcome'], isset($sum['outcome']) ? (string) $sum['outcome'] : 'unknown');
            continue;
        }

        $scans['total']++;
        igh_inc($scans['outcome'], isset($sum['outcome']) ? (string) $sum['outcome'] : 'unknown');
        igh_inc($scans['engagement'], isset($sum['engagement']) ? (string) $sum['engagement'] : 'unknown');
        if (isset($sum['username']) && $sum['username'] === 'missing') {
            $scans['username_missing']++;
        }
        if (isset($sum['profile_pic']) && $sum['profile_pic'] === 'missing') {
            $scans['profile_pic_missing']++;
        }

        $version = ($r['app'] ?: '?') . ' (' . ($r['build'] ?: '?') . ')';
        if (!isset($versions[$version])) {
            $versions[$version] = ['scans' => 0, 'ops' => []];
        }
        $versions[$version]['scans']++;
        foreach ($reportOps as $op => $ok) {
            if (!isset($versions[$version]['ops'][$op])) {
                $versions[$version]['ops'][$op] = ['n' => 0, 'ok' => 0];
            }
            $versions[$version]['ops'][$op]['n']++;
            $versions[$version]['ops'][$op]['ok'] += $ok ? 1 : 0;
        }
        igh_inc($playbooks, $r['pb'] ?: '?');
    }

    return [
        'generated_at' => gmdate('c'),
        'hours'        => $hours,
        'windows'      => $windows,
        'last_report'  => $lastReport ? gmdate('c', $lastReport) : null,
        'ops'          => $ops,
        'timeline'     => $timeline,
        'strategies'   => array_values($strategies),
        'scans'        => $scans,
        'prelogin'     => $prelogin,
        'versions'     => $versions,
        'playbooks'    => $playbooks,
        'alerts'       => igh_alert_state(),
        'channels'     => igh_alert_channels(),
    ];
}

// ── Alerts ──────────────────────────────────────────────────────────────────

function igh_alert_state()
{
    $path = IGH_DIR . '/alerts.json';
    $state = is_readable($path) ? json_decode(file_get_contents($path), true) : null;
    return is_array($state) ? $state : ['evaluated_at' => 0, 'firing' => []];
}

/** Evaluate at most once every IGH_EVAL_EVERY seconds, one process at a time. */
function igh_maybe_evaluate_alerts()
{
    $fp = @fopen(igh_dir() . '/.eval.lock', 'c');
    if (!$fp) {
        return;
    }
    if (flock($fp, LOCK_EX | LOCK_NB)) {
        $state = igh_alert_state();
        $lastEvaluated = isset($state['evaluated_at']) ? (int) $state['evaluated_at'] : 0;
        if (time() - $lastEvaluated >= IGH_EVAL_EVERY) {
            igh_evaluate_alerts($state);
        }
        igh_maybe_send_digest();
        flock($fp, LOCK_UN);
    }
    fclose($fp);
}

/** Once a day, after IGH_DIGEST_HOUR local time. Caller holds the lock. */
function igh_maybe_send_digest()
{
    $path = igh_dir() . '/digest.json';
    $state = is_readable($path) ? json_decode(file_get_contents($path), true) : null;
    $last = (is_array($state) && isset($state['day'])) ? (string) $state['day'] : '';

    $now = new DateTime('now', new DateTimeZone(IGH_DIGEST_TZ));
    $today = $now->format('Y-m-d');
    if ($today === $last || (int) $now->format('G') < IGH_DIGEST_HOUR) {
        return;
    }
    // Written before sending: a failed send must not retry all day.
    @file_put_contents($path, json_encode(['day' => $today]), LOCK_EX);

    $s = igh_summary(24);
    $share = function ($part, $whole) {
        return $whole > 0 ? round($part / $whole * 100) . '%' : '-';
    };
    $lookups = (int) $s['prelogin']['total'];
    $found = isset($s['prelogin']['outcome']['found']) ? (int) $s['prelogin']['outcome']['found'] : 0;
    $scans = (int) $s['scans']['total'];
    $done = isset($s['scans']['outcome']['completed']) ? (int) $s['scans']['outcome']['completed'] : 0;
    $noPhoto = (int) $s['scans']['profile_pic_missing'];

    $weak = [];
    foreach ($s['ops'] as $op => $windows) {
        if (!isset($windows[24]) || $windows[24]['n'] < 30) {
            continue;
        }
        $rate = $windows[24]['ok'] / $windows[24]['n'];
        if ($rate < 0.7) {
            $weak[$op] = round($rate * 100);
        }
    }
    asort($weak);

    $lines = [
        'Unfollow Tracker - the last 24 hours',
        '',
        "Username screen: {$lookups} lookups, " . $share($found, $lookups) . ' found their profile',
        "After login: {$scans} scans, {$done} finished",
        "No profile photo: {$noPhoto} of {$scans} (" . $share($noPhoto, $scans) . ')',
    ];
    if ($weak) {
        $parts = [];
        foreach ($weak as $op => $rate) {
            $parts[] = "{$op} {$rate}%";
        }
        $lines[] = '';
        $lines[] = 'Working less than 70%: ' . implode(' · ', $parts);
    }
    $lines[] = '';
    $lines[] = 'Panel: ' . IGH_PANEL_URL;
    igh_notify(implode("\n", $lines));
}

function igh_evaluate_alerts(array $state)
{
    $now = time();
    $windowStart = $now - IGH_ALERT_WINDOW;
    $records = igh_load($windowStart - 86400);

    $win = [];
    $base = [];
    $winStrategy = [];
    $baseStrategy = [];
    foreach ($records as $r) {
        $inWindow = $r['t'] >= $windowStart;
        foreach (igh_report_ops($r) as $op => $ok) {
            if ($inWindow) {
                igh_inc($win[$op], 'n');
                igh_inc($win[$op], 'ok', $ok ? 1 : 0);
            } else {
                igh_inc($base[$op], 'n');
                igh_inc($base[$op], 'ok', $ok ? 1 : 0);
            }
        }
        foreach ($r['ev'] as $e) {
            if ($e['o'] === 'skipped') {
                continue;
            }
            $k = $e['op'] . '|' . $e['s'];
            $failed = in_array($e['o'], IGH_SUCCESS, true) ? 0 : $e['n'];
            if ($inWindow) {
                igh_inc($winStrategy[$k], 'n', $e['n']);
                igh_inc($winStrategy[$k], 'fail', $failed);
                if ($failed) {
                    igh_inc($winStrategy[$k], 'o:' . $e['o'] . ($e['st'] ? ' ' . $e['st'] : ''), $e['n']);
                }
            } else {
                igh_inc($baseStrategy[$k], 'n', $e['n']);
                igh_inc($baseStrategy[$k], 'fail', $failed);
            }
        }
    }

    $firing = [];
    $evaluated = [];

    // Critical: the operation itself is failing (every strategy, including backups).
    foreach ($win as $op => $w) {
        if ($w['n'] < IGH_ALERT_MIN_SAMPLE) {
            continue;
        }
        $key = "op:$op";
        $evaluated[$key] = true;
        $rate = $w['ok'] / $w['n'];
        $baseRate = (isset($base[$op]) && $base[$op]['n'] >= IGH_BASELINE_MIN_SAMPLE) ? $base[$op]['ok'] / $base[$op]['n'] : null;
        if ($rate < 0.3 || ($rate < 0.5 && ($baseRate === null || $baseRate - $rate >= 0.2))) {
            $firing[$key] = [
                'level'   => 'critical',
                'what'    => $op,
                'message' => sprintf('%s is failing: %d%% of %d scans succeeded in the last 30 min%s.',
                    $op, round($rate * 100), $w['n'],
                    $baseRate === null ? '' : sprintf(' (normally %d%%)', round($baseRate * 100))),
            ];
        }
    }

    // Warning: one strategy broke while a backup still carries the operation —
    // fix it before the backup breaks too.
    foreach (IGH_STRATEGY_ALERTS ? $winStrategy : [] as $k => $w) {
        if ($w['n'] < IGH_ALERT_MIN_SAMPLE) {
            continue;
        }
        list($op, $strategy) = explode('|', $k, 2);
        $key = "strategy:$k";
        $evaluated[$key] = true;
        if (isset($firing["op:$op"])) {
            continue;
        }
        $failRate = $w['fail'] / $w['n'];
        $b = isset($baseStrategy[$k]) ? $baseStrategy[$k] : null;
        $baseFail = ($b && $b['n'] >= IGH_BASELINE_MIN_SAMPLE) ? $b['fail'] / $b['n'] : null;
        if ($failRate >= 0.5 && ($baseFail === null || $baseFail < 0.2)) {
            $reasons = [];
            foreach ($w as $name => $count) {
                if (strpos($name, 'o:') === 0) {
                    $reasons[substr($name, 2)] = $count;
                }
            }
            arsort($reasons);
            $firing[$key] = [
                'level'   => 'warning',
                'what'    => $op . ' → ' . $strategy,
                'message' => sprintf('%s → %s fails %d%% of %d attempts (mostly "%s").',
                    $op, $strategy, round($failRate * 100), $w['n'], (string) key($reasons)),
            ];
        }
    }

    $previous = isset($state['firing']) && is_array($state['firing']) ? $state['firing'] : [];
    if (!IGH_STRATEGY_ALERTS) {
        foreach (array_keys($previous) as $key) {
            if (strpos($key, 'strategy:') === 0) {
                unset($previous[$key]);
            }
        }
    }
    $messages = [];
    foreach ($firing as $key => $alert) {
        if (!isset($previous[$key])) {
            $alert['since'] = $now;
            $alert['notified_at'] = $now;
            $messages[] = ($alert['level'] === 'critical' ? '🔴 ' : '🟠 ') . $alert['message'];
        } else {
            $alert['since'] = $previous[$key]['since'];
            $alert['notified_at'] = $previous[$key]['notified_at'];
            if ($now - $alert['notified_at'] >= IGH_RENOTIFY_EVERY) {
                $alert['notified_at'] = $now;
                $messages[] = '🔴 Still failing: ' . $alert['message'];
            }
        }
        $firing[$key] = $alert;
    }
    foreach ($previous as $key => $alert) {
        if (isset($firing[$key])) {
            continue;
        }
        // Too few scans this window to judge (e.g. overnight): keep the alert
        // open rather than announcing a recovery nobody observed.
        if (!isset($evaluated[$key])) {
            $firing[$key] = $alert;
            continue;
        }
        $messages[] = isset($alert['what'])
            ? '✅ Recovered: ' . $alert['what'] . ' is working again.'
            : '✅ Recovered: ' . $alert['message'];
    }

    @file_put_contents(igh_dir() . '/alerts.json', json_encode(['evaluated_at' => $now, 'firing' => $firing]), LOCK_EX);

    if ($messages) {
        igh_notify("Unfollow Tracker — Instagram health\n\n" . implode("\n\n", $messages) . "\n\nPanel: " . IGH_PANEL_URL);
    }
}

function igh_alert_config()
{
    $path = __DIR__ . '/.ig_health_config.json';
    $config = is_readable($path) ? json_decode(file_get_contents($path), true) : null;
    return is_array($config) ? $config : [];
}

function igh_alert_channels()
{
    $c = igh_alert_config();
    return [
        'telegram' => !empty($c['telegram_bot_token']) && !empty($c['telegram_chat_id']),
        'email'    => !empty($c['email_to']),
    ];
}

function igh_notify($text)
{
    $config = igh_alert_config();
    $sent = false;
    if (!empty($config['telegram_bot_token']) && !empty($config['telegram_chat_id'])) {
        $url = 'https://api.telegram.org/bot' . rawurlencode($config['telegram_bot_token']) . '/sendMessage';
        $body = http_build_query([
            'chat_id' => $config['telegram_chat_id'],
            'text' => $text,
            'disable_web_page_preview' => 'true',
        ]);
        $sent = igh_http_post($url, $body) || $sent;
    }
    if (IGH_EMAIL_ALERTS && !empty($config['email_to'])) {
        $sent = @mail($config['email_to'], 'Unfollow Tracker: Instagram health alert', $text,
            "Content-Type: text/plain; charset=UTF-8\r\n") || $sent;
    }
    return $sent;
}

function igh_http_post($url, $body)
{
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 8,
        ]);
        $response = curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        return $response !== false && $code >= 200 && $code < 300;
    }
    $context = stream_context_create(['http' => [
        'method' => 'POST',
        'header' => "Content-Type: application/x-www-form-urlencoded\r\n",
        'content' => $body,
        'timeout' => 8,
    ]]);
    return @file_get_contents($url, false, $context) !== false;
}
