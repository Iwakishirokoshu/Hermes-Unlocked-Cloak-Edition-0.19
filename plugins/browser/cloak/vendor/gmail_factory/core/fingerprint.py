"""
Fingerprint Module - Browser fingerprint masking for Selenium and Playwright
Includes: WebRTC blocking, Canvas/WebGL noise, Client Hints spoofing, Hardware masking
"""
import random
import logging

logger = logging.getLogger('gmail_creator_fingerprint')

POLTERGEIST_SCRIPT = r"""
(function() {
    /* PRNG - Seeded Random Number Generator (session-unique) */
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
        return [(h1^h2^h3^h4)>>>0, (h2^h1)>>>0, (h3^h1)>>>0, (h4^h1)>>>0];
    }

    function sfc32(a, b, c, d) {
        return function() {
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

    const sessionSeed = "" + navigator.hardwareConcurrency + screen.colorDepth +
                        Date.now() + Math.random() + screen.width + screen.height;
    const seed = cyrb128(sessionSeed);
    const rand = sfc32(seed[0], seed[1], seed[2], seed[3]);

    /* Canvas Noise */
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    Object.defineProperty(HTMLCanvasElement.prototype, 'toDataURL', {
        value: new Proxy(origToDataURL, {
            apply(target, thisArg, args) {
                try {
                    const ctx = thisArg.getContext('2d');
                    if (ctx) {
                        ctx.globalCompositeOperation = 'multiply';
                        ctx.fillStyle = 'rgba(' +
                            Math.floor(rand()*255) + ',' +
                            Math.floor(rand()*255) + ',' +
                            Math.floor(rand()*255) + ',0.0' +
                            Math.floor(rand()*9) + ')';
                        ctx.fillRect(Math.floor(rand()*20), Math.floor(rand()*20), 1, 1);
                    }
                } catch(e) {}
                return Reflect.apply(target, thisArg, args);
            }
        })
    });

    /* Canvas getImageData Noise */
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(sx, sy, sw, sh) {
        const imageData = origGetImageData.call(this, sx, sy, sw, sh);
        for (let i = 0; i < imageData.data.length; i += 4) {
            if (rand() < 0.001) {
                imageData.data[i] = (imageData.data[i] + Math.floor(rand() * 3) - 1) & 255;
                imageData.data[i+1] = (imageData.data[i+1] + Math.floor(rand() * 3) - 1) & 255;
                imageData.data[i+2] = (imageData.data[i+2] + Math.floor(rand() * 3) - 1) & 255;
            }
        }
        return imageData;
    };

    /* WebGL Renderer Spoofing */
    const glHandler = {
        apply: function(target, thisArg, args) {
            const param = args[0];
            const result = Reflect.apply(target, thisArg, args);
            if (param === 37445) return 'Google Inc. (NVIDIA)';
            if (param === 37446) {
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
        const c = document.createElement('canvas');
        const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
        if (gl && gl.getExtension('WEBGL_debug_renderer_info')) {
            gl.getParameter = new Proxy(gl.getParameter, glHandler);
        }
    } catch(e) {}

    /* Client Hints Spoof */
    const chromeVer = ['128','129','130','131','132'][Math.floor(rand()*5)];
    Object.defineProperty(navigator, 'userAgentData', {
        value: {
            brands: [
                {brand: 'Google Chrome', version: chromeVer},
                {brand: 'Chromium', version: chromeVer},
                {brand: 'Not_A Brand', version: '24'}
            ],
            mobile: false,
            platform: 'Windows',
            getHighEntropyValues: async (hints) => ({
                architecture: 'x86', bitness: '64', model: '',
                platformVersion: '10.0.0',
                uaFullVersion: chromeVer + '.0.6778.' + Math.floor(rand()*100),
                fullVersionList: [
                    {brand: 'Google Chrome', version: chromeVer + '.0.6778.' + Math.floor(rand()*100)},
                    {brand: 'Chromium', version: chromeVer + '.0.6778.' + Math.floor(rand()*100)},
                    {brand: 'Not_A Brand', version: '24.0.0.0'}
                ],
                wow64: false,
                brands: [
                    {brand: 'Google Chrome', version: chromeVer},
                    {brand: 'Chromium', version: chromeVer},
                    {brand: 'Not_A Brand', version: '24'}
                ]
            })
        }
    });

    /* Remove WebDriver Detection */
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

    /* Hardware Masking */
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => [4,6,8,12,16][Math.floor(rand()*5)]
    });
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => [4,8,16,32][Math.floor(rand()*4)]
    });
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

    /* Plugins & MimeTypes */
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const p = [
                {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
                {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1},
                {name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 2}
            ];
            p.item = (i) => p[i];
            p.namedItem = (name) => p.find(x => x.name === name);
            p.refresh = () => {};
            return p;
        }
    });
    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => {
            const m = [
                {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'},
                {type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format'}
            ];
            m.item = (i) => m[i];
            m.namedItem = (t) => m.find(x => x.type === t);
            return m;
        }
    });

    /* Language & Platform */
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

    /* Permissions API */
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) => {
        if (params.name === 'notifications') return Promise.resolve({ state: 'prompt', onchange: null });
        return origQuery.call(navigator.permissions, params);
    };

    /* Chrome Runtime Mock */
    window.chrome = {
        runtime: { connect: () => {}, sendMessage: () => {}, onMessage: { addListener: () => {} } },
        loadTimes: () => ({
            requestTime: Date.now()/1000 - rand()*10,
            startLoadTime: Date.now()/1000 - rand()*5,
            commitLoadTime: Date.now()/1000 - rand()*3,
            finishDocumentLoadTime: Date.now()/1000 - rand()*2,
            finishLoadTime: Date.now()/1000 - rand(),
            firstPaintTime: Date.now()/1000 - rand()*4,
            firstPaintAfterLoadTime: 0,
            navigationType: 'Other',
            wasFetchedViaSpdy: true, wasNpnNegotiated: true,
            npnNegotiatedProtocol: 'h2', wasAlternateProtocolAvailable: false,
            connectionInfo: 'h2'
        }),
        csi: () => ({
            startE: Date.now() - Math.floor(rand()*1000),
            onloadT: Date.now() - Math.floor(rand()*500),
            pageT: Math.floor(rand()*5000), tran: 15
        })
    };

    /* Connection API */
    Object.defineProperty(navigator, 'connection', {
        get: () => ({
            effectiveType: '4g',
            rtt: Math.floor(50 + rand()*100),
            downlink: Math.floor(5 + rand()*10),
            saveData: false, onchange: null
        })
    });

    /* Battery API */
    if (navigator.getBattery) {
        navigator.getBattery = () => Promise.resolve({
            charging: true, chargingTime: 0,
            dischargingTime: Infinity, level: 0.85 + rand()*0.15,
            onchargingchange: null, onchargingtimechange: null,
            ondischargingtimechange: null, onlevelchange: null
        });
    }

    /* AudioContext Noise */
    const origAudioCtx = window.AudioContext || window.webkitAudioContext;
    if (origAudioCtx) {
        const origCreateOsc = origAudioCtx.prototype.createOscillator;
        origAudioCtx.prototype.createOscillator = function() {
            const osc = origCreateOsc.call(this);
            const origConnect = osc.connect.bind(osc);
            osc.connect = function(dest) {
                if (osc.frequency) {
                    const f = osc.frequency.value || 440;
                    osc.frequency.value = f + (rand() - 0.5) * 0.001;
                }
                return origConnect(dest);
            };
            return osc;
        };
    }

    /* Console Protection */
    const origLog = console.log;
    console.log = function(...args) {
        const str = args.join(' ').toLowerCase();
        if (str.includes('fingerprint') || str.includes('webdriver') || str.includes('automation')) return;
        return origLog.apply(console, args);
    };
})();
"""

WEBRTC_BLOCK_SCRIPT = """
Object.defineProperty(navigator, 'mediaDevices', {
    get: () => ({
        getUserMedia: () => Promise.reject(new Error('NotAllowedError')),
        enumerateDevices: () => Promise.resolve([])
    })
});
window.RTCPeerConnection = function() {
    return { close: () => {}, createDataChannel: () => ({}) };
};
window.webkitRTCPeerConnection = window.RTCPeerConnection;
window.mozRTCPeerConnection = window.RTCPeerConnection;
"""

AUDIO_SPOOF_SCRIPT = """
const originalGetChannelData = AudioBuffer.prototype.getChannelData;
AudioBuffer.prototype.getChannelData = function(channel) {
    const data = originalGetChannelData.call(this, channel);
    for (let i = 0; i < data.length; i++) {
        data[i] = data[i] + (Math.random() * 0.0001 - 0.00005);
    }
    return data;
};
"""


def get_poltergeist_script():
    return POLTERGEIST_SCRIPT


def inject_selenium_fingerprint(driver):
    """Inject all fingerprint masking scripts via Selenium CDP."""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": WEBRTC_BLOCK_SCRIPT
        })

        random_cores = random.choice([4, 6, 8, 12])
        random_memory = random.choice([4, 8, 16])
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"""
                Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {random_cores} }});
                Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {random_memory} }});
                Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => 0 }});
                if (navigator.getBattery) {{
                    navigator.getBattery = () => Promise.resolve({{
                        charging: true, chargingTime: 0,
                        dischargingTime: Infinity, level: 1.0
                    }});
                }}
            """
        })

        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": AUDIO_SPOOF_SCRIPT
        })

        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                const originalQuery = Permissions.prototype.query;
                Permissions.prototype.query = function(parameters) {
                    if (parameters.name === 'notifications') {
                        return Promise.resolve({ state: 'denied' });
                    }
                    return originalQuery.apply(this, arguments);
                };
            """
        })

        logger.info(f"Selenium fingerprint injected: {random_cores} cores, {random_memory}GB")
        return True
    except Exception as e:
        logger.warning(f"Selenium fingerprint injection error: {e}")
        return False


def inject_selenium_poltergeist(driver):
    """Inject the full Poltergeist script via Selenium execute_script."""
    try:
        driver.execute_script(POLTERGEIST_SCRIPT)
        logger.info("Poltergeist fingerprint applied via execute_script")
        return True
    except Exception as e:
        logger.warning(f"Poltergeist injection error: {e}")
        return False
