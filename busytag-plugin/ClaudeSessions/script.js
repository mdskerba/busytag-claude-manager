// Claude Sessions plugin - shared logic.
//
// The busytag-claude daemon (with "transport": "app") decides what the tag shows. This polls
// GET /frame on it once a second through the host HTTP bridge and replays the result with the
// plugin API: the PNG with setImage, the LED ring with setLedColor. LED patterns (the working
// spinner, the needs-input flash) are stepped here, and a finished pattern is reported back
// with `done=<led_seq>` on the next poll.

const DEFAULT_CONFIG = { port: 47621 };
const POLL_MS = 1000;
const MS_PER_FADE_UNIT = 3; // the device's fade "speed" is roughly 3 ms per unit
const ONLY_THIS_LED = 128;  // mask bit: light only these LEDs, the rest off

function loadConfiguration(input) {
    const merged = Object.assign({}, DEFAULT_CONFIG, input || {});
    merged.port = parseInt(merged.port, 10) || DEFAULT_CONFIG.port;
    return merged;
}

async function fetchFrame(port, haveImage, done) {
    let url = 'http://127.0.0.1:' + port + '/frame?';
    if (haveImage != null) url += 'have=' + haveImage + '&';
    if (done != null) url += 'done=' + done;
    const res = await busyTagPlugin.http.request({ url: url, method: 'GET' });
    if (res.status !== 200) throw new Error('daemon answered ' + res.status);
    return JSON.parse(res.body);
}

const LED_COUNT = 7;
let ring = new Array(LED_COUNT).fill('000000'); // what the LEDs show now

// Sets each LED to its colour in `colors`, one setLedColor per colour. Sent without
// setLedColor's dedupe key, so the parts of a split ring can't swallow each other.
function showRing(colors) {
    const masks = {};
    colors.forEach(function (c, i) { masks[c] = (masks[c] || 0) | (1 << i); });
    Object.keys(masks).forEach(function (c) {
        busyTagPlugin.sendCommand('setLedColor', { color: '#' + c.toUpperCase(), ledBits: masks[c] });
    });
    ring = colors.slice();
}

// One pattern step, with the device's mask rules: LEDs outside the mask keep their colour,
// unless the ONLY_THIS_LED bit turns them off.
function setRing(color, mask) {
    showRing(ring.map(function (c, i) {
        return mask & (1 << i) ? color : (mask & ONLY_THIS_LED ? '000000' : c);
    }));
}

// Plays [[mask, color, speed, delay], ...] `repeat` times (255 = forever), then calls onEnd.
// Returns a function that stops it.
function playSteps(steps, repeat, onEnd) {
    let i = 0, laps = 0, timer = null, stopped = false;
    function step() {
        if (stopped) return;
        if (i === steps.length) {
            i = 0;
            laps++;
            if (repeat !== 255 && laps >= repeat) {
                onEnd();
                return;
            }
        }
        const [mask, color, speed, delay] = steps[i++];
        setRing(color, mask);
        timer = setTimeout(step, Math.max(50, speed * MS_PER_FADE_UNIT + delay));
    }
    step();
    return function () {
        stopped = true;
        clearTimeout(timer);
    };
}

// Shown on the tag when the daemon can't be reached.
function drawOffline(canvas, message) {
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.textAlign = 'center';
    ctx.fillStyle = '#D97757';
    ctx.font = 'bold 22px Arial';
    ctx.fillText('Claude', canvas.width / 2, 120);
    ctx.fillStyle = '#888888';
    ctx.font = '14px Arial';
    ctx.fillText(message, canvas.width / 2, 150);
}

function canvasToBase64Png(canvas) {
    const dataUrl = canvas.toDataURL('image/png');
    return dataUrl.substring(dataUrl.indexOf(',') + 1);
}
