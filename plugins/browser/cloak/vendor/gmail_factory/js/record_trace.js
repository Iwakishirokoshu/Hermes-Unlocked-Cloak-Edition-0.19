/* ============================================
   HUMAN TRACE RECORDER
   
   Run this script on a REAL browser session to 
   record genuine human behavior patterns.
   
   Save output to data/human_trace.json
============================================ */

(function () {
    'use strict';

    const trace = [];
    const startTime = Date.now();

    // Record mouse movements
    document.addEventListener('mousemove', (e) => {
        trace.push({
            t: Date.now() - startTime,
            type: 'mouse',
            x: e.clientX,
            y: e.clientY,
            mx: e.movementX,
            my: e.movementY
        });
    }, { passive: true });

    // Record mouse clicks
    document.addEventListener('click', (e) => {
        trace.push({
            t: Date.now() - startTime,
            type: 'click',
            x: e.clientX,
            y: e.clientY,
            button: e.button
        });
    });

    // Record key presses (without capturing sensitive data)
    document.addEventListener('keydown', (e) => {
        // Only record timing and key type, not actual characters for passwords
        const isSensitive = e.target.type === 'password';
        trace.push({
            t: Date.now() - startTime,
            type: 'keydown',
            key: isSensitive ? '*' : e.key,
            code: e.code,
            shift: e.shiftKey,
            ctrl: e.ctrlKey,
            alt: e.altKey
        });
    });

    document.addEventListener('keyup', (e) => {
        const isSensitive = e.target.type === 'password';
        trace.push({
            t: Date.now() - startTime,
            type: 'keyup',
            key: isSensitive ? '*' : e.key,
            code: e.code
        });
    });

    // Record scrolling
    let lastScrollTime = 0;
    window.addEventListener('scroll', () => {
        const now = Date.now();
        if (now - lastScrollTime > 50) { // Throttle to 20fps
            trace.push({
                t: now - startTime,
                type: 'scroll',
                x: window.scrollX,
                y: window.scrollY
            });
            lastScrollTime = now;
        }
    }, { passive: true });

    // Record focus changes
    document.addEventListener('focus', (e) => {
        if (e.target.tagName) {
            trace.push({
                t: Date.now() - startTime,
                type: 'focus',
                tag: e.target.tagName,
                id: e.target.id || null,
                name: e.target.name || null
            });
        }
    }, true);

    // Record touch events (for mobile)
    document.addEventListener('touchstart', (e) => {
        if (e.touches[0]) {
            trace.push({
                t: Date.now() - startTime,
                type: 'touch',
                x: e.touches[0].clientX,
                y: e.touches[0].clientY
            });
        }
    }, { passive: true });

    // Export function
    window.exportTrace = function () {
        const blob = new Blob([JSON.stringify(trace, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'human_trace.json';
        a.click();
        URL.revokeObjectURL(url);
        console.log(`Exported ${trace.length} events`);
    };

    // Auto-save to console after 2 minutes
    setTimeout(() => {
        console.log('=== Human Trace Recording Complete ===');
        console.log(`Total events: ${trace.length}`);
        console.log('Call window.exportTrace() to download');
        console.log('Or copy from below:');
        console.log(JSON.stringify(trace));
    }, 120000);

    console.log('🔴 Recording human trace... Move mouse, click, type, scroll naturally.');
    console.log('   Call window.exportTrace() when done.');
})();
