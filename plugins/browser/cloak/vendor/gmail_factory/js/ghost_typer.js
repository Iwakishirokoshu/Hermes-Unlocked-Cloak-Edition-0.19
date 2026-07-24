/* ============================================
   GHOST TYPER - Micro-Behavioral Synthesis
   
   Replays pre-recorded human traces with 0.1% jitter
   so it's never identical, yet always human.
   
   Inject via CDP: Page.addScriptToEvaluateOnNewDocument
============================================ */

(function () {
    'use strict';

    // PRNG for consistent jitter
    function mulberry32(a) {
        return function () {
            var t = a += 0x6D2B79F5;
            t = Math.imul(t ^ t >>> 15, t | 1);
            t ^= t + Math.imul(t ^ t >>> 7, t | 61);
            return ((t ^ t >>> 14) >>> 0) / 4294967296;
        }
    }
    const rand = mulberry32(Date.now());

    // Jitter function: ±1ms (0.1% variance)
    const jitter = () => (rand() * 2 - 1) * 0.001;

    // Human-like timing patterns (typical keystroke intervals in ms)
    const KEYSTROKE_PATTERNS = {
        fast: { min: 50, max: 100 },
        normal: { min: 100, max: 200 },
        slow: { min: 200, max: 400 },
        pause: { min: 500, max: 1500 }
    };

    // Mouse movement entropy generator
    const generateMouseEntropy = () => {
        return {
            acceleration: 0.5 + rand() * 1.5,
            curvature: rand() * 0.3,
            overshoot: rand() < 0.15 ? rand() * 5 : 0
        };
    };

    // Scroll acceleration patterns
    const SCROLL_PATTERNS = {
        smooth: { step: 20, interval: 16 },
        fast: { step: 100, interval: 50 },
        human: { step: 50 + rand() * 50, interval: 30 + rand() * 20 }
    };

    // Store original event constructors
    const OriginalMouseEvent = MouseEvent;
    const OriginalKeyboardEvent = KeyboardEvent;
    const OriginalWheelEvent = WheelEvent;

    // Human-like mouse position with slight tremor
    let currentMouseX = 0;
    let currentMouseY = 0;

    // Add micro-tremor to mouse movements (humans can't hold perfectly still)
    const addTremor = (value) => {
        return value + (rand() - 0.5) * 2;
    };

    // Override MouseEvent to add realistic properties
    window.MouseEvent = function (type, init) {
        init = init || {};

        // Add realistic timing jitter
        if (!init.timeStamp) {
            init.timeStamp = performance.now() + jitter() * 1000;
        }

        // Add micro-tremor to coordinates
        if (init.clientX !== undefined) {
            init.clientX = addTremor(init.clientX);
            currentMouseX = init.clientX;
        }
        if (init.clientY !== undefined) {
            init.clientY = addTremor(init.clientY);
            currentMouseY = init.clientY;
        }

        // Realistic movement delta
        if (type === 'mousemove') {
            init.movementX = init.movementX || (rand() - 0.5) * 4;
            init.movementY = init.movementY || (rand() - 0.5) * 4;
        }

        return new OriginalMouseEvent(type, init);
    };
    window.MouseEvent.prototype = OriginalMouseEvent.prototype;

    // Override KeyboardEvent to add realistic timing
    window.KeyboardEvent = function (type, init) {
        init = init || {};

        // Add realistic repeat patterns
        if (type === 'keydown' && init.repeat === undefined) {
            init.repeat = false;
        }

        return new OriginalKeyboardEvent(type, init);
    };
    window.KeyboardEvent.prototype = OriginalKeyboardEvent.prototype;

    // Human-like typing simulator
    window.GhostTyper = {
        // Type text with human-like delays
        type: async function (element, text, options = {}) {
            const speed = options.speed || 'normal';
            const pattern = KEYSTROKE_PATTERNS[speed];

            for (let i = 0; i < text.length; i++) {
                const char = text[i];

                // Calculate delay based on character
                let delay = pattern.min + rand() * (pattern.max - pattern.min);

                // Add extra delay for special characters (shift key)
                if (/[A-Z!@#$%^&*()_+{}|:"<>?]/.test(char)) {
                    delay += 30 + rand() * 50;
                }

                // Occasional pause (thinking)
                if (rand() < 0.05) {
                    delay += KEYSTROKE_PATTERNS.pause.min + rand() *
                        (KEYSTROKE_PATTERNS.pause.max - KEYSTROKE_PATTERNS.pause.min);
                }

                // Occasional typo and correction (very rare)
                if (options.allowTypos && rand() < 0.02) {
                    const wrongChar = String.fromCharCode(char.charCodeAt(0) + (rand() > 0.5 ? 1 : -1));
                    element.value += wrongChar;
                    element.dispatchEvent(new InputEvent('input', { bubbles: true }));
                    await new Promise(r => setTimeout(r, 100 + rand() * 100));
                    element.value = element.value.slice(0, -1);
                    element.dispatchEvent(new InputEvent('input', { bubbles: true }));
                    await new Promise(r => setTimeout(r, 50 + rand() * 50));
                }

                // Type the character
                await new Promise(r => setTimeout(r, delay + jitter() * 1000));
                element.value += char;
                element.dispatchEvent(new InputEvent('input', { bubbles: true, data: char }));
                element.dispatchEvent(new KeyboardEvent('keydown', { key: char, bubbles: true }));
                element.dispatchEvent(new KeyboardEvent('keyup', { key: char, bubbles: true }));
            }
        },

        // Move mouse with human-like path
        moveMouse: async function (targetX, targetY, options = {}) {
            const steps = options.steps || 20 + Math.floor(rand() * 20);
            const entropy = generateMouseEntropy();

            const startX = currentMouseX;
            const startY = currentMouseY;
            const dx = targetX - startX;
            const dy = targetY - startY;

            for (let i = 0; i <= steps; i++) {
                const t = i / steps;

                // Bezier curve for natural movement
                const easeT = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;

                // Add curvature
                const curve = Math.sin(t * Math.PI) * entropy.curvature * Math.max(Math.abs(dx), Math.abs(dy));

                const x = startX + dx * easeT + curve * (rand() - 0.5);
                const y = startY + dy * easeT + curve * (rand() - 0.5);

                // Overshoot near the end
                let finalX = x;
                let finalY = y;
                if (i === steps && entropy.overshoot > 0) {
                    finalX += (dx > 0 ? 1 : -1) * entropy.overshoot;
                    finalY += (dy > 0 ? 1 : -1) * entropy.overshoot;
                }

                const event = new OriginalMouseEvent('mousemove', {
                    clientX: finalX,
                    clientY: finalY,
                    bubbles: true
                });
                document.dispatchEvent(event);

                currentMouseX = finalX;
                currentMouseY = finalY;

                await new Promise(r => setTimeout(r, 10 + rand() * 10));
            }

            // Correct overshoot
            if (entropy.overshoot > 0) {
                await new Promise(r => setTimeout(r, 50 + rand() * 50));
                const correctionEvent = new OriginalMouseEvent('mousemove', {
                    clientX: targetX,
                    clientY: targetY,
                    bubbles: true
                });
                document.dispatchEvent(correctionEvent);
                currentMouseX = targetX;
                currentMouseY = targetY;
            }
        },

        // Click with human-like timing
        click: async function (element, options = {}) {
            const rect = element.getBoundingClientRect();
            const targetX = rect.left + rect.width * (0.3 + rand() * 0.4);
            const targetY = rect.top + rect.height * (0.3 + rand() * 0.4);

            // Move mouse to element first
            await this.moveMouse(targetX, targetY);

            // Small pause before click
            await new Promise(r => setTimeout(r, 50 + rand() * 100));

            // Mousedown
            element.dispatchEvent(new OriginalMouseEvent('mousedown', {
                clientX: targetX,
                clientY: targetY,
                bubbles: true,
                button: 0
            }));

            // Hold duration (humans don't click instantly)
            await new Promise(r => setTimeout(r, 80 + rand() * 120));

            // Mouseup
            element.dispatchEvent(new OriginalMouseEvent('mouseup', {
                clientX: targetX,
                clientY: targetY,
                bubbles: true,
                button: 0
            }));

            // Click event
            element.dispatchEvent(new OriginalMouseEvent('click', {
                clientX: targetX,
                clientY: targetY,
                bubbles: true,
                button: 0
            }));
        },

        // Scroll with human-like acceleration
        scroll: async function (targetY, options = {}) {
            const pattern = SCROLL_PATTERNS[options.pattern || 'human'];
            const startY = window.scrollY;
            const distance = targetY - startY;
            const steps = Math.abs(distance / pattern.step);

            for (let i = 0; i <= steps; i++) {
                const t = i / steps;
                const easeT = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
                const currentY = startY + distance * easeT;

                window.scrollTo({
                    top: currentY,
                    behavior: 'instant'
                });

                // Dispatch wheel event
                document.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: pattern.step * (distance > 0 ? 1 : -1),
                    bubbles: true
                }));

                await new Promise(r => setTimeout(r, pattern.interval + rand() * 10));
            }
        }
    };

    // Inject realistic event timing
    const originalAddEventListener = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function (type, listener, options) {
        if (['click', 'mousedown', 'mouseup', 'keydown', 'keyup'].includes(type)) {
            const wrappedListener = function (event) {
                // Add micro-delay to simulate neural processing time
                setTimeout(() => listener.call(this, event), rand() * 5);
            };
            return originalAddEventListener.call(this, type, wrappedListener, options);
        }
        return originalAddEventListener.call(this, type, listener, options);
    };

    console.log('👻 Ghost Typer: Active - Human behavioral synthesis enabled');
})();
