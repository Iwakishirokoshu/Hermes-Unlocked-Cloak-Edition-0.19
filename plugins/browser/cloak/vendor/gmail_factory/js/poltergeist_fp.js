/* ============================================
   POLTERGEIST FINGERPRINT SYSTEM
   Zero-Trust, Session-Unique Fingerprinting
   
   Inject via CDP: Page.addScriptToEvaluateOnNewDocument
============================================ */

/* 0.0 - PRNG Functions (Seeded Random Number Generator) */
function cyrb128(str) {
    let h1 = 1779033703, h2 = 3144134277,
        h3 = 1013904242, h4 = 2773480762;
    for (let i = 0, k; i < str.length; i++) {
        k = str.charCodeAt(i);
        h1 = h2 ^ Math.imul(h1 ^ k, 597399067);
        h2 = h3 ^ Math.imul(h2 ^ k, 2869860233);
        h3 = h4 ^ Math.imul(h3 ^ k, 951274213);
        h4 = h1 ^ Math.imul(h4 ^ k, 2716044179);
    }
    h1 = Math.imul(h3 ^ (h1 >>> 18), 597399067);
    h2 = Math.imul(h4 ^ (h2 >>> 22), 2869860233);
    h3 = Math.imul(h1 ^ (h3 >>> 17), 951274213);
    h4 = Math.imul(h2 ^ (h4 >>> 19), 2716044179);
    return [(h1 ^ h2 ^ h3 ^ h4) >>> 0, (h2 ^ h1) >>> 0, (h3 ^ h1) >>> 0, (h4 ^ h1) >>> 0];
}

function sfc32(a, b, c, d) {
    return function () {
        a >>>= 0; b >>>= 0; c >>>= 0; d >>>= 0;
        var t = (a + b) | 0;
        a = b ^ b >>> 9;
        b = c + (c << 3) | 0;
        c = (c << 21 | c >>> 11);
        d = d + 1 | 0;
        t = t + d | 0;
        c = c + t | 0;
        return (t >>> 0) / 4294967296;
    }
}

/* 1.0 - Initialize session-unique seed */
const sessionSeed = "" + navigator.hardwareConcurrency + screen.colorDepth +
    Date.now() + Math.random() + screen.width + screen.height;
const seed = cyrb128(sessionSeed);
const rand = sfc32(seed[0], seed[1], seed[2], seed[3]);

/* 1.1 - RNG-Seeded Canvas Noise (deterministic per session) */
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
Object.defineProperty(HTMLCanvasElement.prototype, 'toDataURL', {
    value: new Proxy(originalToDataURL, {
        apply(target, thisArg, args) {
            try {
                const ctx = thisArg.getContext('2d');
                if (ctx) {
                    ctx.globalCompositeOperation = 'multiply';
                    ctx.fillStyle = `rgba(${Math.floor(rand() * 255)},${Math.floor(rand() * 255)},${Math.floor(rand() * 255)},0.0${Math.floor(rand() * 9)})`;
                    ctx.fillRect(Math.floor(rand() * 20), Math.floor(rand() * 20), 1, 1);
                }
            } catch (e) { }
            return Reflect.apply(target, thisArg, args);
        }
    })
});

/* 1.2 - Canvas getImageData Noise */
const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
CanvasRenderingContext2D.prototype.getImageData = function (sx, sy, sw, sh) {
    const imageData = originalGetImageData.call(this, sx, sy, sw, sh);
    for (let i = 0; i < imageData.data.length; i += 4) {
        if (rand() < 0.001) {
            imageData.data[i] = (imageData.data[i] + Math.floor(rand() * 3) - 1) & 255;
            imageData.data[i + 1] = (imageData.data[i + 1] + Math.floor(rand() * 3) - 1) & 255;
            imageData.data[i + 2] = (imageData.data[i + 2] + Math.floor(rand() * 3) - 1) & 255;
        }
    }
    return imageData;
};

/* 1.3 - WebGL Fingerprint Noise */
const getParameterProxyHandler = {
    apply: function (target, thisArg, args) {
        const param = args[0];
        const result = Reflect.apply(target, thisArg, args);

        if (param === 37445) { // UNMASKED_VENDOR_WEBGL
            return 'Google Inc. (NVIDIA)';
        }
        if (param === 37446) { // UNMASKED_RENDERER_WEBGL
            const renderers = [
                'ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11)',
                'ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)',
                'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)',
                'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)'
            ];
            return renderers[Math.floor(rand() * renderers.length)];
        }
        return result;
    }
};

try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (gl) {
        const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
        if (debugInfo) {
            gl.getParameter = new Proxy(gl.getParameter, getParameterProxyHandler);
        }
    }
} catch (e) { }

/* 2.0 - Client-Hints Spoof (Chrome 110+) */
const chromeVersions = ['128', '129', '130', '131', '132'];
const selectedVersion = chromeVersions[Math.floor(rand() * chromeVersions.length)];

Object.defineProperty(navigator, 'userAgentData', {
    value: {
        brands: [
            { brand: 'Google Chrome', version: selectedVersion },
            { brand: 'Chromium', version: selectedVersion },
            { brand: 'Not_A Brand', version: '24' }
        ],
        mobile: false,
        platform: 'Windows',
        getHighEntropyValues: async (hints) => ({
            architecture: 'x86',
            bitness: '64',
            model: '',
            platformVersion: '10.0.0',
            uaFullVersion: selectedVersion + '.0.6778.' + Math.floor(rand() * 100),
            fullVersionList: [
                { brand: 'Google Chrome', version: selectedVersion + '.0.6778.' + Math.floor(rand() * 100) },
                { brand: 'Chromium', version: selectedVersion + '.0.6778.' + Math.floor(rand() * 100) },
                { brand: 'Not_A Brand', version: '24.0.0.0' }
            ],
            wow64: false
        })
    }
});

/* 3.0 - TLS/Crypto Proxy */
Object.defineProperty(window, 'crypto', {
    value: new Proxy(window.crypto, {
        get(target, prop) {
            if (prop === 'getRandomValues') return target.getRandomValues.bind(target);
            if (prop === 'randomUUID') return target.randomUUID.bind(target);
            if (prop === 'subtle') return target.subtle;
            return Reflect.get(target, prop);
        }
    })
});

/* 4.0 - Remove WebDriver Detection */
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true
});

delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

/* 5.0 - Mock Plugins */
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1 },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 2 }
        ];
        plugins.item = (i) => plugins[i];
        plugins.namedItem = (name) => plugins.find(p => p.name === name);
        plugins.refresh = () => { };
        return plugins;
    }
});

/* 5.1 - Mock MimeTypes */
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => {
        const mimes = [
            { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
            { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
        ];
        mimes.item = (i) => mimes[i];
        mimes.namedItem = (type) => mimes.find(m => m.type === type);
        return mimes;
    }
});

/* 6.0 - Mock Navigator Properties */
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => [4, 6, 8, 12, 16][Math.floor(rand() * 5)] });
Object.defineProperty(navigator, 'deviceMemory', { get: () => [4, 8, 16, 32][Math.floor(rand() * 4)] });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

/* 7.0 - Mock Permissions API */
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => {
    if (parameters.name === 'notifications') {
        return Promise.resolve({ state: 'prompt', onchange: null });
    }
    return originalQuery.call(navigator.permissions, parameters);
};

/* 8.0 - Mock Chrome Runtime */
window.chrome = {
    runtime: {
        connect: () => { },
        sendMessage: () => { },
        onMessage: { addListener: () => { } }
    },
    loadTimes: () => ({
        requestTime: Date.now() / 1000 - rand() * 10,
        startLoadTime: Date.now() / 1000 - rand() * 5,
        commitLoadTime: Date.now() / 1000 - rand() * 3,
        finishDocumentLoadTime: Date.now() / 1000 - rand() * 2,
        finishLoadTime: Date.now() / 1000 - rand(),
        firstPaintTime: Date.now() / 1000 - rand() * 4,
        firstPaintAfterLoadTime: 0,
        navigationType: 'Other',
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
        npnNegotiatedProtocol: 'h2',
        wasAlternateProtocolAvailable: false,
        connectionInfo: 'h2'
    }),
    csi: () => ({
        startE: Date.now() - Math.floor(rand() * 1000),
        onloadT: Date.now() - Math.floor(rand() * 500),
        pageT: Math.floor(rand() * 5000),
        tran: 15
    })
};

/* 9.0 - Mock Connection API */
Object.defineProperty(navigator, 'connection', {
    get: () => ({
        effectiveType: '4g',
        rtt: Math.floor(50 + rand() * 100),
        downlink: Math.floor(5 + rand() * 10),
        saveData: false,
        onchange: null
    })
});

/* 10.0 - Mock Battery API */
if (navigator.getBattery) {
    navigator.getBattery = () => Promise.resolve({
        charging: true,
        chargingTime: 0,
        dischargingTime: Infinity,
        level: 0.85 + rand() * 0.15,
        onchargingchange: null,
        onchargingtimechange: null,
        ondischargingtimechange: null,
        onlevelchange: null
    });
}

/* 11.0 - Screen Properties */
const screenProps = {
    width: screen.width,
    height: screen.height,
    availWidth: screen.availWidth,
    availHeight: screen.availHeight,
    colorDepth: screen.colorDepth,
    pixelDepth: screen.pixelDepth
};

Object.defineProperty(window, 'screen', {
    value: new Proxy(screen, {
        get(target, prop) {
            if (prop in screenProps) return screenProps[prop];
            return Reflect.get(target, prop);
        }
    })
});

/* 12.0 - AudioContext Fingerprint Noise */
const originalAudioContext = window.AudioContext || window.webkitAudioContext;
if (originalAudioContext) {
    const originalCreateOscillator = originalAudioContext.prototype.createOscillator;
    originalAudioContext.prototype.createOscillator = function () {
        const oscillator = originalCreateOscillator.call(this);
        const originalConnect = oscillator.connect.bind(oscillator);
        oscillator.connect = function (destination) {
            if (oscillator.frequency) {
                const currentFreq = oscillator.frequency.value || 440;
                oscillator.frequency.value = currentFreq + (rand() - 0.5) * 0.001;
            }
            return originalConnect(destination);
        };
        return oscillator;
    };
}

console.log('🎭 Poltergeist FP: Active [Session: ' + seed[0].toString(16).toUpperCase() + ']');
