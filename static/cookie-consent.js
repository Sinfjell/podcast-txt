/**
 * First-party analytics consent for Podskrift / PostHog.
 *
 * Choice is stored in localStorage and a same-site cookie (12 months). That
 * cookie is necessary to remember Accept/Decline — not for tracking.
 *
 * PostHog itself is initialised from base.html. Before Accept it uses
 * persistence 'memory' + opt_out_capturing_by_default so it sets no cookies
 * and captures nothing (including no session recording). We do not use
 * cookieless aggregate pageviews without consent: PostHog still processes IP
 * / UA as personal data under GDPR, and Decline must mean no analytics.
 */
(function (global) {
    'use strict';

    var COOKIE_NAME = 'podskrift_cookie_consent';
    var STORAGE_KEY = 'podskrift_cookie_consent';
    var ACCEPTED = 'accepted';
    var DECLINED = 'declined';
    var MAX_AGE_SEC = 365 * 24 * 60 * 60; // 12 months

    function readCookie(name) {
        try {
            var parts = (document.cookie || '').split(';');
            for (var i = 0; i < parts.length; i++) {
                var p = parts[i].trim();
                if (p.indexOf(name + '=') === 0) {
                    return decodeURIComponent(p.slice(name.length + 1));
                }
            }
        } catch (e) { /* ignore */ }
        return null;
    }

    function writeCookie(value) {
        try {
            var secure = (location.protocol === 'https:') ? '; Secure' : '';
            document.cookie = (
                COOKIE_NAME + '=' + encodeURIComponent(value)
                + '; Path=/; Max-Age=' + MAX_AGE_SEC
                + '; SameSite=Lax' + secure
            );
        } catch (e) { /* ignore */ }
    }

    function readStorage() {
        try {
            return localStorage.getItem(STORAGE_KEY);
        } catch (e) {
            return null;
        }
    }

    function writeStorage(value) {
        try {
            localStorage.setItem(STORAGE_KEY, value);
        } catch (e) { /* private mode */ }
    }

    function normalize(value) {
        if (value === ACCEPTED || value === DECLINED) return value;
        return null;
    }

    function getChoice() {
        return normalize(readStorage()) || normalize(readCookie(COOKIE_NAME));
    }

    function setChoice(value) {
        var v = normalize(value);
        if (!v) return;
        writeStorage(v);
        writeCookie(v);
    }

    function bannerEl() {
        return document.getElementById('cookieConsent');
    }

    function setBannerOpen(open) {
        var el = bannerEl();
        if (!el) return;
        if (open) {
            el.hidden = false;
            el.setAttribute('data-open', '1');
            document.body.classList.add('cookie-banner-open');
        } else {
            el.hidden = true;
            el.removeAttribute('data-open');
            document.body.classList.remove('cookie-banner-open');
        }
    }

    function showBanner() {
        setBannerOpen(true);
    }

    function hideBanner() {
        setBannerOpen(false);
    }

    function bind(handlers) {
        handlers = handlers || {};
        var accept = document.getElementById('cookieConsentAccept');
        var decline = document.getElementById('cookieConsentDecline');
        if (accept) {
            accept.addEventListener('click', function () {
                setChoice(ACCEPTED);
                hideBanner();
                if (typeof handlers.onAccept === 'function') handlers.onAccept();
            });
        }
        if (decline) {
            decline.addEventListener('click', function () {
                setChoice(DECLINED);
                hideBanner();
                if (typeof handlers.onDecline === 'function') handlers.onDecline();
            });
        }
        var reopen = document.getElementById('cookieSettingsLink');
        if (reopen) {
            reopen.addEventListener('click', function (ev) {
                ev.preventDefault();
                showBanner();
                if (typeof handlers.onReopen === 'function') handlers.onReopen();
            });
        }
    }

    global.PodskriftConsent = {
        COOKIE_NAME: COOKIE_NAME,
        STORAGE_KEY: STORAGE_KEY,
        ACCEPTED: ACCEPTED,
        DECLINED: DECLINED,
        MAX_AGE_SEC: MAX_AGE_SEC,
        getChoice: getChoice,
        setChoice: setChoice,
        showBanner: showBanner,
        hideBanner: hideBanner,
        bind: bind
    };
})(window);
