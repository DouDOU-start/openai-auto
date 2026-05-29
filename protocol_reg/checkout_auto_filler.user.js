// ==UserScript==
// @name         PayPal Auto Filler
// @namespace    http://tampermonkey.net/
// @version      32.0
// @description  Auto-fill PayPal/OpenAI checkout pages
// @match        https://www.paypal.com/*
// @match        https://pay.openai.com/*
// @match        https://checkout.stripe.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @connect      meiguodizhi.com
// @connect      mail-api.yuecheng.shop
// @run-at       document-idle
// ==/UserScript==

// ========== 配置 ==========
var CONFIG = {
    phone: '9012345678', // 日本手机号（去掉 +81 后的本地号码）
    cardExpiry: '03 / 30', // 有效期
    cardCvv: '996', // CVV
    cardNumber: '1234561234568888', // 卡号兜底
    smsPollSeconds: 2,
    smsTimeoutSeconds: 180,
    smsNumbers: [],
    smsLease: null
};
// ========================

let cachedProfile = null;
let selectedSmsNumber = null;
let smsLeaseAcquiring = false;
let smsLeaseReleased = false;
let smsLeaseWaitStartedAt = 0;
let securityCodeHandling = false;
let smsPollingActive = false;
let lastSecurityFillAt = 0;
let securityFillAttempts = 0;
let securityAutoStopped = false;
let debugPanelNode = null;
let debugLogLines = [];
let securityWatcherTicks = 0;
var JP_FALLBACK_ADDRESS = { street:'2-8-1 Nishishinjuku', city:'Shinjuku-ku', state:'Tokyo', stateNative:'Tokyo-to', stateCode:'13', zip:'163-8001' };
var JP_PREFECTURE_ALIASES = {
    '01': { name:'Hokkaido', native:'Hokkaido', local:'北海道' },
    '02': { name:'Aomori', native:'Aomori-ken', local:'青森県' },
    '03': { name:'Iwate', native:'Iwate-ken', local:'岩手県' },
    '04': { name:'Miyagi', native:'Miyagi-ken', local:'宮城県' },
    '05': { name:'Akita', native:'Akita-ken', local:'秋田県' },
    '06': { name:'Yamagata', native:'Yamagata-ken', local:'山形県' },
    '07': { name:'Fukushima', native:'Fukushima-ken', local:'福島県' },
    '08': { name:'Ibaraki', native:'Ibaraki-ken', local:'茨城県' },
    '09': { name:'Tochigi', native:'Tochigi-ken', local:'栃木県' },
    '10': { name:'Gunma', native:'Gunma-ken', local:'群馬県' },
    '11': { name:'Saitama', native:'Saitama-ken', local:'埼玉県' },
    '12': { name:'Chiba', native:'Chiba-ken', local:'千葉県' },
    '13': { name:'Tokyo', native:'Tokyo-to', local:'東京都' },
    '14': { name:'Kanagawa', native:'Kanagawa-ken', local:'神奈川県' },
    '15': { name:'Niigata', native:'Niigata-ken', local:'新潟県' },
    '16': { name:'Toyama', native:'Toyama-ken', local:'富山県' },
    '17': { name:'Ishikawa', native:'Ishikawa-ken', local:'石川県' },
    '18': { name:'Fukui', native:'Fukui-ken', local:'福井県' },
    '19': { name:'Yamanashi', native:'Yamanashi-ken', local:'山梨県' },
    '20': { name:'Nagano', native:'Nagano-ken', local:'長野県' },
    '21': { name:'Gifu', native:'Gifu-ken', local:'岐阜県' },
    '22': { name:'Shizuoka', native:'Shizuoka-ken', local:'静岡県' },
    '23': { name:'Aichi', native:'Aichi-ken', local:'愛知県' },
    '24': { name:'Mie', native:'Mie-ken', local:'三重県' },
    '25': { name:'Shiga', native:'Shiga-ken', local:'滋賀県' },
    '26': { name:'Kyoto', native:'Kyoto-fu', local:'京都府' },
    '27': { name:'Osaka', native:'Osaka-fu', local:'大阪府' },
    '28': { name:'Hyogo', native:'Hyogo-ken', local:'兵庫県' },
    '29': { name:'Nara', native:'Nara-ken', local:'奈良県' },
    '30': { name:'Wakayama', native:'Wakayama-ken', local:'和歌山県' },
    '31': { name:'Tottori', native:'Tottori-ken', local:'鳥取県' },
    '32': { name:'Shimane', native:'Shimane-ken', local:'島根県' },
    '33': { name:'Okayama', native:'Okayama-ken', local:'岡山県' },
    '34': { name:'Hiroshima', native:'Hiroshima-ken', local:'広島県' },
    '35': { name:'Yamaguchi', native:'Yamaguchi-ken', local:'山口県' },
    '36': { name:'Tokushima', native:'Tokushima-ken', local:'徳島県' },
    '37': { name:'Kagawa', native:'Kagawa-ken', local:'香川県' },
    '38': { name:'Ehime', native:'Ehime-ken', local:'愛媛県' },
    '39': { name:'Kochi', native:'Kochi-ken', local:'高知県' },
    '40': { name:'Fukuoka', native:'Fukuoka-ken', local:'福岡県' },
    '41': { name:'Saga', native:'Saga-ken', local:'佐賀県' },
    '42': { name:'Nagasaki', native:'Nagasaki-ken', local:'長崎県' },
    '43': { name:'Kumamoto', native:'Kumamoto-ken', local:'熊本県' },
    '44': { name:'Oita', native:'Oita-ken', local:'大分県' },
    '45': { name:'Miyazaki', native:'Miyazaki-ken', local:'宮崎県' },
    '46': { name:'Kagoshima', native:'Kagoshima-ken', local:'鹿児島県' },
    '47': { name:'Okinawa', native:'Okinawa-ken', local:'沖縄県' }
};

(function() {
    'use strict';
    var log = function(s) {
        var text = String(s || '');
        console.log('[PP] ' + text);
        debugLogLines.push(new Date().toLocaleTimeString() + ' ' + text);
        if (debugLogLines.length > 12) debugLogLines = debugLogLines.slice(-12);
        renderDebugPanel();
    };
    applyRuntimeConfig();
    ensureDebugPanel();
    log('自动填充脚本已注入，版本 32.0-debug');

    // 隐藏验证码和地址补全
    var st = document.createElement('style');
    st.textContent = '#captcha-standalone,.captcha-overlay,.captcha-container,.AddressAutocomplete-results{display:none!important;height:0!important;overflow:hidden!important}';
    document.head.appendChild(st);

    function ensureDebugPanel() {
        if (debugPanelNode || !currentHost().includes('paypal.com')) {
            return;
        }
        debugPanelNode = document.createElement('div');
        debugPanelNode.id = 'protocol-reg-checkout-debug';
        debugPanelNode.style.cssText = [
            'position:fixed',
            'left:12px',
            'bottom:12px',
            'z-index:2147483647',
            'width:min(520px,calc(100vw - 24px))',
            'max-height:38vh',
            'overflow:auto',
            'background:rgba(14,17,20,.92)',
            'color:#d7fff6',
            'border:1px solid rgba(87,255,222,.35)',
            'border-radius:12px',
            'box-shadow:0 12px 40px rgba(0,0,0,.35)',
            'padding:10px 12px',
            'font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
            'white-space:pre-wrap',
            'pointer-events:none'
        ].join(';');
        (document.body || document.documentElement).appendChild(debugPanelNode);
        renderDebugPanel();
    }

    function renderDebugPanel() {
        if (!debugPanelNode) {
            ensureDebugPanel();
        }
        if (!debugPanelNode) {
            return;
        }
        var sms = currentSmsNumber();
        var prompt = false;
        try {
            prompt = hasSecurityCodePromptContent();
        } catch (e) {}
        var otpCount = 0;
        try {
            otpCount = otpTargetsIn(document).length;
        } catch (e) {}
        var activeName = '-';
        try {
            activeName = elementDebugName(document.activeElement);
        } catch (e) {}
        debugPanelNode.textContent = [
            'Protocol Reg Checkout Debug',
            'host=' + currentHost() + ' path=' + currentPath(),
            'smsPhone=' + (sms.phone || '-') + ' smsUrl=' + (sms.smsUrl ? maskUrl(sms.smsUrl) : '-'),
            'smsLease=' + smsLeaseStatusText(),
            'securityPrompt=' + prompt + ' handling=' + securityCodeHandling + ' watcherTicks=' + securityWatcherTicks,
            'otpInputs=' + otpCount + ' promptText=' + securityPromptTextMatches(pageText()),
            'active=' + activeName,
            'smsPolling=' + smsPollingActive,
            'fillAttempts=' + securityFillAttempts,
            'autoStopped=' + securityAutoStopped,
            'cachedCode=' + (cachedSecurityCode ? 'yes' : 'no'),
            '---',
            debugLogLines.join('\n')
        ].join('\n');
    }

    // 随机邮箱
    function randEmail() {
        var c = 'abcdefghijklmnopqrstuvwxyz0123456789', e = '';
        for (var i = 0; i < 17; i++) e += c[Math.floor(Math.random() * c.length)];
        return e + '@gmail.com';
    }

    // 随机密码
    function randPass() {
        var L = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
        var D = '0123456789', S = '!@#$%^', A = L + D + S;
        var p = L[Math.floor(Math.random()*26)] + L[26+Math.floor(Math.random()*26)] + D[Math.floor(Math.random()*10)] + S[Math.floor(Math.random()*6)];
        for (var i = 4; i < 14; i++) p += A[Math.floor(Math.random()*A.length)];
        return p.split('').sort(function(){return Math.random()-0.5}).join('');
    }

    // 填写input
    function fill(id, val) {
        var el = document.getElementById(id);
        if (!el) { log('未找到字段：' + id); return false; }
        fillElement(el, val);
        log('已填写字段：' + id + ' = ' + el.value);
        return true;
    }

    function fillIfPresent(id, val) {
        var el = document.getElementById(id);
        if (!el) return false;
        if (fieldText(el) === String(val == null ? '' : val).trim()) return false;
        fillElement(el, val);
        log('已填写字段：' + id + ' = ' + el.value);
        return true;
    }

    function fillElementIfChanged(el, val, label) {
        if (!el) return false;
        var before = fieldText(el);
        if (before === String(val == null ? '' : val).trim()) return false;
        fillElement(el, val);
        var after = fieldText(el);
        if (after !== before) {
            log('已填写字段：' + (label || elementDebugName(el)) + ' = ' + after);
            return true;
        }
        return false;
    }

    // 按选择器填写
    function fillSel(sel, val) {
        var el = document.querySelector(sel);
        if (!el) { log('未找到选择器：' + sel); return false; }
        fillElement(el, val);
        log('已填写选择器：' + sel + ' = ' + el.value);
        return true;
    }

    function fillElement(el, val) {
        var value = String(val == null ? '' : val);
        try { el.focus({ preventScroll: true }); } catch (e) {
            try { el.focus(); } catch (_) {}
        }
        var proto = null;
        if (el instanceof HTMLInputElement) proto = HTMLInputElement.prototype;
        else if (el instanceof HTMLTextAreaElement) proto = HTMLTextAreaElement.prototype;
        else if (el instanceof HTMLSelectElement) proto = HTMLSelectElement.prototype;
        var desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
        try {
            el.dispatchEvent(new InputEvent('beforeinput', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertReplacementText',
                data: value
            }));
        } catch (e) {}
        if (desc && typeof desc.set === 'function') {
            desc.set.call(el, value);
        } else {
            el.value = value;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
    }

    function applyRuntimeConfig() {
        var runtime = window.__PROTOCOL_REG_CHECKOUT_CONFIG__ || {};
        var sms = runtime.checkoutSms || {};
        var numbers = Array.isArray(sms.numbers) ? sms.numbers : [];
        CONFIG.smsNumbers = numbers.map(function(item) {
            return {
                phone: String(item.phone || item.number || '').trim(),
                smsUrl: String(item.smsUrl || item.sms_url || item.url || '').trim(),
                label: String(item.label || item.name || '').trim()
            };
        }).filter(function(item) {
            return item.phone && item.smsUrl;
        });
        CONFIG.smsTimeoutSeconds = Number(sms.timeoutSeconds || sms.timeout_seconds || sms.timeout || CONFIG.smsTimeoutSeconds) || CONFIG.smsTimeoutSeconds;
        CONFIG.smsPollSeconds = Number(sms.pollSeconds || sms.poll_seconds || sms.poll || CONFIG.smsPollSeconds) || CONFIG.smsPollSeconds;
        var lease = sms.lease || {};
        CONFIG.smsLease = lease && lease.acquireUrl && lease.releaseUrl && lease.token ? {
            acquireUrl: String(lease.acquireUrl || '').trim(),
            releaseUrl: String(lease.releaseUrl || '').trim(),
            token: String(lease.token || '').trim(),
            waitSeconds: Number(lease.waitSeconds || lease.wait_seconds || 25) || 25
        } : null;
        if (CONFIG.smsNumbers.length) {
            if (CONFIG.smsLease) {
                log('当前：已加载 checkout 短信配置 ' + CONFIG.smsNumbers.length + ' 组，将在手机验证步骤排队取号');
            } else {
                selectedSmsNumber = CONFIG.smsNumbers[0];
                CONFIG.phone = selectedSmsNumber.phone;
                log('当前：已加载 checkout 短信配置 ' + CONFIG.smsNumbers.length + ' 组，使用 ' + CONFIG.phone);
            }
        } else {
            log('当前：未配置 checkout 短信收码，使用脚本默认手机号');
        }
    }

    function currentSmsNumber() {
        if (selectedSmsNumber && selectedSmsNumber.phone && selectedSmsNumber.smsUrl) {
            return selectedSmsNumber;
        }
        if (!CONFIG.smsLease && CONFIG.smsNumbers && CONFIG.smsNumbers.length) {
            selectedSmsNumber = CONFIG.smsNumbers[0];
            CONFIG.phone = selectedSmsNumber.phone;
            return selectedSmsNumber;
        }
        return {
            phone: CONFIG.phone,
            smsUrl: '',
            label: ''
        };
    }

    function configuredPhone() {
        return currentSmsNumber().phone || CONFIG.phone;
    }

    function fillPhoneWithLease(cb) {
        ensureCheckoutSmsLease(function(sms) {
            if (!sms || !sms.phone) {
                log('未获取到短信号码，停止提交并等待人工处理');
                cb && cb(false);
                return;
            }
            fill('phone', sms.phone);
            cb && cb(true);
        });
    }

    function ensureCheckoutSmsLease(cb, retries) {
        retries = retries || 0;
        if (selectedSmsNumber && selectedSmsNumber.phone && selectedSmsNumber.smsUrl) {
            cb(selectedSmsNumber);
            return;
        }
        if (!CONFIG.smsLease) {
            cb(currentSmsNumber());
            return;
        }
        if (smsLeaseAcquiring) {
            if (retries % 5 === 0) {
                log('等待：短信号码正在排队获取中');
            }
            setTimeout(function() { ensureCheckoutSmsLease(cb, retries + 1); }, 1000);
            return;
        }
        smsLeaseAcquiring = true;
        smsLeaseReleased = false;
        smsLeaseWaitStartedAt = Date.now();
        requestCheckoutSmsLease(cb);
    }

    function requestCheckoutSmsLease(cb) {
        var lease = CONFIG.smsLease || {};
        var waitSeconds = Math.max(1, Number(lease.waitSeconds || 25) || 25);
        var url = lease.acquireUrl + (lease.acquireUrl.indexOf('?') === -1 ? '?' : '&') + 'wait=' + encodeURIComponent(waitSeconds) + '&_ts=' + Date.now();
        smsLeaseAcquiring = true;
        log('当前：进入手机验证步骤，开始排队获取短信号码');
        httpRequest({
            method: 'POST',
            url: url,
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ token: lease.token, numbers: leaseRequestNumbers() }),
            onload: function(r) {
                smsLeaseAcquiring = false;
                try {
                    var d = JSON.parse(r.responseText || '{}');
                    if (Number(r.status || 0) >= 400) {
                        throw new Error(apiErrorMessage(d, r.status));
                    }
                    var item = d.number || {};
                    if (!item.phone || !item.smsUrl) {
                        throw new Error(apiErrorMessage(d, r.status) || '接口未返回可用号码');
                    }
                    selectedSmsNumber = {
                        phone: String(item.phone || '').trim(),
                        smsUrl: String(item.smsUrl || item.sms_url || '').trim(),
                        label: String(item.label || '').trim(),
                        leaseId: String(d.leaseId || '')
                    };
                    CONFIG.phone = selectedSmsNumber.phone;
                    log('已获取短信号码租约：' + CONFIG.phone + '，等待 ' + (d.waitSeconds || 0) + ' 秒');
                    cb(selectedSmsNumber);
                } catch (e) {
                    log('短信号码租约解析失败：' + e.message);
                    handleCheckoutSmsLeaseFailure(cb, e.message);
                }
            },
            onerror: function(e) {
                log('短信号码暂不可用或租约请求失败：' + (e.statusText || 'network error'));
                handleCheckoutSmsLeaseFailure(cb, e.statusText || 'network error');
            }
        });
    }

    function apiErrorMessage(data, status) {
        var d = data || {};
        var detail = d.detail || d.message || d.error || d.msg || '';
        if (Array.isArray(detail)) {
            detail = detail.map(function(item) {
                return item && item.msg ? item.msg : String(item || '');
            }).join('; ');
        }
        detail = String(detail || '').trim();
        return detail || (status ? 'HTTP ' + status : '');
    }

    function retryCheckoutSmsLease(cb) {
        var elapsed = Date.now() - smsLeaseWaitStartedAt;
        var timeoutMs = Math.max(30000, (Number(CONFIG.smsTimeoutSeconds) || 180) * 1000);
        if (elapsed >= timeoutMs) {
            smsLeaseAcquiring = false;
            log('等待结束：超过短信配置等待时间，未获取到可用号码');
            cb(fallbackSmsNumberAfterLeaseFailure());
            return;
        }
        log('等待：3 秒后继续排队获取短信号码');
        setTimeout(function() {
            requestCheckoutSmsLease(cb);
        }, 3000);
    }

    function handleCheckoutSmsLeaseFailure(cb, reason) {
        var text = String(reason || '');
        if (/未登录|会话已过期|token 无效|403|401|400/i.test(text)) {
            smsLeaseAcquiring = false;
            cb(fallbackSmsNumberAfterLeaseFailure());
            return;
        }
        retryCheckoutSmsLease(cb);
    }

    function releaseCheckoutSmsLease(reason) {
        var lease = CONFIG.smsLease || {};
        if (!lease.releaseUrl || !lease.token || smsLeaseReleased) {
            return;
        }
        smsLeaseReleased = true;
        log('当前：释放短信号码租约 reason=' + (reason || 'done'));
        httpRequest({
            method: 'POST',
            url: lease.releaseUrl,
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ token: lease.token }),
            onload: function() {},
            onerror: function(e) {
                log('短信号码租约释放失败：' + (e.statusText || 'network error'));
            }
        });
    }

    function leaseRequestNumbers() {
        return (CONFIG.smsNumbers || []).map(function(item) {
            return {
                phone: item.phone,
                sms_url: item.smsUrl,
                label: item.label || ''
            };
        });
    }

    function fallbackSmsNumberAfterLeaseFailure() {
        if (CONFIG.smsNumbers && CONFIG.smsNumbers.length) {
            selectedSmsNumber = CONFIG.smsNumbers[0];
            CONFIG.phone = selectedSmsNumber.phone;
            log('降级：使用本地第一组短信号码 ' + CONFIG.phone + '，并发时可能需要手动确认');
            return selectedSmsNumber;
        }
        return null;
    }

    function smsLeaseStatusText() {
        if (!CONFIG.smsLease) return 'local';
        if (selectedSmsNumber && !smsLeaseReleased) return 'leased';
        if (smsLeaseAcquiring) return 'waiting';
        if (smsLeaseReleased) return 'released';
        return 'not-acquired';
    }

    function jpPrefecture(value) {
        var raw = String(value || '').trim();
        if (!raw) return JP_PREFECTURE_ALIASES[JP_FALLBACK_ADDRESS.stateCode];
        var upper = raw.toUpperCase();
        var normalizedCode = /^\d$/.test(upper) ? '0' + upper : upper;
        if (JP_PREFECTURE_ALIASES[normalizedCode]) return JP_PREFECTURE_ALIASES[normalizedCode];
        for (var code in JP_PREFECTURE_ALIASES) {
            if (!Object.prototype.hasOwnProperty.call(JP_PREFECTURE_ALIASES, code)) {
                continue;
            }
            var pref = JP_PREFECTURE_ALIASES[code];
            if (pref.name.toLowerCase() === raw.toLowerCase() ||
                pref.native.toLowerCase() === raw.toLowerCase() ||
                String(pref.local || '').toLowerCase() === raw.toLowerCase() ||
                (pref.name + '-to').toLowerCase() === raw.toLowerCase() ||
                (pref.name + '-fu').toLowerCase() === raw.toLowerCase() ||
                (pref.name + '-ken').toLowerCase() === raw.toLowerCase()) {
                return pref;
            }
        }
        return JP_PREFECTURE_ALIASES[JP_FALLBACK_ADDRESS.stateCode];
    }

    function jpPrefectureNative(value) {
        return jpPrefecture(value).native;
    }

    function jpPrefectureLocal(value) {
        return jpPrefecture(value).local;
    }

    function jpPrefectureCode(value) {
        var raw = String(value || '').trim();
        var normalizedCode = /^\d$/.test(raw) ? '0' + raw : raw;
        if (JP_PREFECTURE_ALIASES[normalizedCode]) return normalizedCode;
        var pref = jpPrefecture(raw);
        for (var code in JP_PREFECTURE_ALIASES) {
            if (Object.prototype.hasOwnProperty.call(JP_PREFECTURE_ALIASES, code) &&
                JP_PREFECTURE_ALIASES[code] === pref) {
                return code;
            }
        }
        return JP_FALLBACK_ADDRESS.stateCode;
    }

    function jpPostalCode(value) {
        var text = String(value || '').trim();
        var m = text.match(/\b(\d{3})[-\s]?(\d{4})\b/);
        return m ? (m[1] + '-' + m[2]) : JP_FALLBACK_ADDRESS.zip;
    }

    function firstText(values, fallback) {
        for (var i = 0; i < values.length; i++) {
            var text = String(values[i] || '').trim();
            if (text) return text;
        }
        return fallback;
    }

    function normalizeJpAddress(addr) {
        // PayPal 对 city/prefecture/postal code 的一致性校验很严，支付页固定使用一组确定有效的日本地址。
        var pref = jpPrefecture(JP_FALLBACK_ADDRESS.stateCode);
        return {
            street: JP_FALLBACK_ADDRESS.street,
            city: JP_FALLBACK_ADDRESS.city,
            state: pref.name,
            stateNative: pref.native,
            stateLocal: pref.local,
            stateCode: JP_FALLBACK_ADDRESS.stateCode,
            zip: jpPostalCode(JP_FALLBACK_ADDRESS.zip)
        };
    }

    function ensureCountryJP(id) {
        var el = document.getElementById(id);
        if (!el) { log('未找到国家字段：' + id); return; }
        var before = el.value;
        fillSelect(id, 'JP');
        if (String(el.value || '').toUpperCase() !== 'JP') {
            fillSelect(id, 'Japan');
        }
        if (el.value === before && String(el.value || '').toUpperCase() !== 'JP') {
            el.value = 'JP';
            el.dispatchEvent(new Event('change', { bubbles: true }));
            log('当前：国家字段已强制切到 JP');
        }
    }

    function ensureAnyCountryJP(ids) {
        var changed = false;
        for (var i = 0; i < ids.length; i++) {
            var el = document.getElementById(ids[i]);
            if (!el) continue;
            if (String(el.value || '').toUpperCase() !== 'JP') {
                ensureCountryJP(ids[i]);
                changed = true;
            }
        }
        return changed;
    }

    var JP_IDENTITY = {
        firstKana: 'タロウ',
        lastKana: 'ヤマダ',
        firstKanji: '太郎',
        lastKanji: '山田',
        birthIso: '1990-01-01',
        birthSlash: '1990/01/01'
    };

    function fillJapaneseNames() {
        var filled = false;
        filled = fillIfPresent('full-name', JP_IDENTITY.lastKanji + ' ' + JP_IDENTITY.firstKanji) || filled;
        filled = fillIfPresent('countrySpecificFirstName', JP_IDENTITY.firstKana) || filled;
        filled = fillIfPresent('countrySpecificLastName', JP_IDENTITY.lastKana) || filled;
        filled = fillIfPresent('kanaFirstName', JP_IDENTITY.firstKana) || filled;
        filled = fillIfPresent('kanaLastName', JP_IDENTITY.lastKana) || filled;
        filled = fillIfPresent('firstNameKana', JP_IDENTITY.firstKana) || filled;
        filled = fillIfPresent('lastNameKana', JP_IDENTITY.lastKana) || filled;
        filled = fillIfPresent('firstName', JP_IDENTITY.firstKanji) || filled;
        filled = fillIfPresent('lastName', JP_IDENTITY.lastKanji) || filled;
        filled = fillSemanticJapaneseNameFields() || filled;
        return filled;
    }

    function fillJapaneseBirthdate() {
        var fields = birthdateFields();
        var filled = false;
        for (var i = 0; i < fields.length; i++) {
            filled = fillBirthdateElement(fields[i]) || filled;
        }
        if (!fields.length) {
            filled = fillIfPresent('dateOfBirth', JP_IDENTITY.birthIso) ||
                     fillIfPresent('birthdate', JP_IDENTITY.birthIso) ||
                     fillIfPresent('dob', JP_IDENTITY.birthIso);
        }
        return filled;
    }

    function fillSemanticJapaneseNameFields() {
        var filled = false;
        var fields = japaneseNameFields();
        for (var i = 0; i < fields.length; i++) {
            var el = fields[i];
            var kind = japaneseNameKind(el);
            if (!kind) continue;
            var script = preferredJapaneseNameScript(el);
            if (!script) continue;
            filled = fillJapaneseNameElement(el, kind, script) || filled;
        }
        return fillJapaneseNameRowsByPosition(fields) || filled;
    }

    function fillJapaneseNameRowsByPosition(fields) {
        var rows = groupNameRows(fields || japaneseNameFields());
        var filled = false;
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            if (!row.first && !row.last) continue;
            var script = row.script || '';
            if (!script && rows.length >= 2) {
                script = i === 0 ? 'kana' : 'kanji';
            }
            if (!script) script = 'kanji';
            if (row.first) filled = fillJapaneseNameElement(row.first, 'first', script) || filled;
            if (row.last) filled = fillJapaneseNameElement(row.last, 'last', script) || filled;
        }
        return filled;
    }

    function fillJapaneseNameElement(el, kind, script) {
        var value = '';
        if (kind === 'full') {
            value = script === 'kana'
                ? JP_IDENTITY.lastKana + ' ' + JP_IDENTITY.firstKana
                : JP_IDENTITY.lastKanji + ' ' + JP_IDENTITY.firstKanji;
        } else if (kind === 'first') {
            value = script === 'kana' ? JP_IDENTITY.firstKana : JP_IDENTITY.firstKanji;
        } else if (kind === 'last') {
            value = script === 'kana' ? JP_IDENTITY.lastKana : JP_IDENTITY.lastKanji;
        }
        if (!value) return false;
        return fillElementIfChanged(el, value, 'JP name ' + kind + '/' + script);
    }

    function japaneseNameFields() {
        var nodes = Array.prototype.slice.call(document.querySelectorAll('input, textarea'));
        return uniqueElements(nodes.filter(function(el) {
            if (!isVisibleElement(el) || el.disabled || el.readOnly) return false;
            var type = String(el.type || '').toLowerCase();
            if (/^(hidden|email|password|button|submit|checkbox|radio|file|search)$/.test(type)) return false;
            var directMeta = [
                inputMeta(el),
                labelTextFor(el),
                el.getAttribute('placeholder') || '',
                el.getAttribute('aria-label') || ''
            ].join(' ');
            if (isNonNameFieldMeta(directMeta)) return false;
            return /countrySpecific|firstName|lastName|given|family|surname|full[-_\s]?name|kana|kanji|phonetic|furigana|フリガナ|ふりがな|かな|カナ|ひらがな|カタカナ|漢字|氏名|名前|姓|名/i.test(directMeta) ||
                   /^(名|姓)$/.test(String(el.getAttribute('placeholder') || '').trim());
        }));
    }

    function isNonNameFieldMeta(meta) {
        return /email|mail|card|cc-|credit|cvv|cvc|expiry|expire|expiration|address|billingLine|billingAddress|city|locality|postal|zip|phone|mobile|password|security|otp|one-time|birth|bday|dob|date|country|state|prefecture|administrative|captcha/i.test(String(meta || ''));
    }

    function japaneseNameKind(el) {
        var idName = [
            el.id || '',
            el.name || '',
            el.autocomplete || '',
            el.getAttribute('data-testid') || ''
        ].join(' ');
        var placeholder = String(el.getAttribute('placeholder') || '').trim();
        var labels = labelTextFor(el);
        var meta = [idName, placeholder, labels, el.getAttribute('aria-label') || ''].join(' ');
        if (/full[-_\s]?name|fullName|氏名/i.test(meta)) return 'full';
        if (/last|family|surname|countrySpecificLast|姓/i.test(meta) || placeholder === '姓') return 'last';
        if (/first|given|countrySpecificFirst|名/i.test(meta) || placeholder === '名') return 'first';
        return '';
    }

    function japaneseNameScript(el) {
        var meta = nameElementText(el);
        if (/countrySpecific|kana|phonetic|furigana|フリガナ|ふりがな|かな|カナ|ひらがな|カタカナ/i.test(meta)) {
            return 'kana';
        }
        if (/kanji|漢字/i.test(meta)) {
            return 'kanji';
        }
        return '';
    }

    function preferredJapaneseNameScript(el) {
        var meta = inputMeta(el);
        var exact = String(el.id || el.name || '').trim();
        if (/countrySpecific|kana|phonetic|furigana/i.test(meta)) return 'kana';
        if (/^(firstName|lastName)$/i.test(exact)) return 'kanji';
        return japaneseNameScript(el);
    }

    function groupNameRows(fields) {
        var sorted = (fields || []).filter(function(el) {
            return japaneseNameKind(el) && japaneseNameKind(el) !== 'full';
        }).sort(inputPositionSort);
        var rows = [];
        for (var i = 0; i < sorted.length; i++) {
            var el = sorted[i];
            var rect = el.getBoundingClientRect();
            var row = rows.length ? rows[rows.length - 1] : null;
            if (!row || Math.abs(row.top - rect.top) > 28) {
                row = { top: rect.top, fields: [], first: null, last: null, script: '' };
                rows.push(row);
            }
            row.fields.push(el);
            var kind = japaneseNameKind(el);
            if (kind === 'first') row.first = el;
            if (kind === 'last') row.last = el;
            row.script = row.script || preferredJapaneseNameScript(el);
        }
        return rows.filter(function(row) {
            return row.fields.length > 0;
        });
    }

    function nameElementText(el) {
        var parts = [
            inputMeta(el),
            labelTextFor(el)
        ];
        var node = el;
        for (var depth = 0; node && depth < 5; depth += 1) {
            var text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
            if (text && text.length <= 280) parts.push(text);
            node = node.parentElement;
        }
        return parts.join(' ');
    }

    function labelTextFor(el) {
        var parts = [];
        try {
            if (el.labels) {
                Array.prototype.forEach.call(el.labels, function(label) {
                    parts.push(String(label.innerText || label.textContent || '').trim());
                });
            }
        } catch (e) {}
        var id = el && el.id ? el.id : '';
        if (id) {
            try {
                Array.prototype.forEach.call(document.querySelectorAll('label[for="' + cssEscape(id) + '"]'), function(label) {
                    parts.push(String(label.innerText || label.textContent || '').trim());
                });
            } catch (e) {}
        }
        var labelledBy = el && el.getAttribute ? String(el.getAttribute('aria-labelledby') || '') : '';
        labelledBy.split(/\s+/).forEach(function(ref) {
            var node = ref ? document.getElementById(ref) : null;
            if (node) parts.push(String(node.innerText || node.textContent || '').trim());
        });
        return parts.filter(Boolean).join(' ');
    }

    function cssEscape(value) {
        if (window.CSS && typeof window.CSS.escape === 'function') {
            return window.CSS.escape(value);
        }
        return String(value || '').replace(/"/g, '\\"');
    }

    function birthdateFields() {
        var selectors = [
            '#dateOfBirth',
            '#birthdate',
            '#birthDate',
            '#dob',
            'input[name="dateOfBirth"]',
            'input[name="birthdate"]',
            'input[name="birthDate"]',
            'input[name="dob"]',
            'input[autocomplete="bday"]',
            'input[id*="birth" i]',
            'input[name*="birth" i]',
            'input[id*="dob" i]',
            'input[name*="dob" i]',
            'input[placeholder*="生年月日"]',
            'input[aria-label*="生年月日"]'
        ];
        var found = [];
        for (var i = 0; i < selectors.length; i++) {
            try {
                Array.prototype.forEach.call(document.querySelectorAll(selectors[i]), function(el) {
                    found.push(el);
                });
            } catch (e) {}
        }
        Array.prototype.forEach.call(document.querySelectorAll('input'), function(el) {
            var meta = [inputMeta(el), labelTextFor(el), String(el.parentElement ? (el.parentElement.innerText || '') : '')].join(' ');
            if (/date of birth|birthdate|birthday|生年月日/i.test(meta)) found.push(el);
        });
        return uniqueElements(found).filter(function(el) {
            return isVisibleElement(el) && !el.disabled && !el.readOnly;
        });
    }

    function fillBirthdateElement(el) {
        if (!birthdateNeedsRepair(el)) {
            return true;
        }
        var values = birthdateCandidates(el);
        if (!values.length) return false;
        writeBirthdateValue(el, values[0], 0);
        scheduleBirthdateFallbacks(el, values, 1);
        return fieldText(el) !== '';
    }

    function birthdateCandidates(el) {
        var type = String(el.type || el.getAttribute('type') || '').toLowerCase();
        if (type === 'date') {
            return [JP_IDENTITY.birthIso];
        }
        return [
            '19900101',
            JP_IDENTITY.birthSlash,
            JP_IDENTITY.birthIso,
            '1990年01月01日',
            '01/01/1990'
        ];
    }

    function writeBirthdateValue(el, value, index) {
        var type = String(el.type || el.getAttribute('type') || '').toLowerCase();
        if (type === 'date') {
            fillElementIfChanged(el, value, index ? 'birthdate fallback' : 'birthdate');
        } else {
            typeTextLikeUser(el, value, index ? 'birthdate fallback' : 'birthdate');
        }
    }

    function scheduleBirthdateFallbacks(el, values, index) {
        if (index >= values.length) return;
        setTimeout(function() {
            if (!birthdateNeedsRepair(el)) {
                return;
            }
            writeBirthdateValue(el, values[index], index);
            scheduleBirthdateFallbacks(el, values, index + 1);
        }, 550);
    }

    function typeTextLikeUser(el, value, label) {
        if (!el) return false;
        var text = String(value == null ? '' : value);
        try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
        try { el.focus({ preventScroll: true }); } catch (e) {
            try { el.focus(); } catch (_) {}
        }
        fillElement(el, '');
        for (var i = 0; i < text.length; i++) {
            var ch = text.charAt(i);
            dispatchKey(el, 'keydown', ch);
            dispatchKey(el, 'keypress', ch);
            dispatchBeforeInput(el, ch);
            setInputValue(el, String(el.value || '') + ch);
            dispatchInput(el, ch);
            dispatchKey(el, 'keyup', ch);
        }
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        log('已模拟输入字段：' + (label || elementDebugName(el)) + ' = ' + fieldText(el));
        return true;
    }

    function setInputValue(el, value) {
        var proto = null;
        if (el instanceof HTMLInputElement) proto = HTMLInputElement.prototype;
        else if (el instanceof HTMLTextAreaElement) proto = HTMLTextAreaElement.prototype;
        else if (el instanceof HTMLSelectElement) proto = HTMLSelectElement.prototype;
        var desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
        if (desc && typeof desc.set === 'function') {
            desc.set.call(el, value);
        } else {
            el.value = value;
        }
    }

    function birthdateValueLooksAccepted(el) {
        if (!el) return false;
        var value = fieldText(el);
        if (!value) return false;
        if (el.validity && el.validity.valid === false) return false;
        if (String(el.getAttribute('aria-invalid') || '').toLowerCase() === 'true') return false;
        return true;
    }

    function birthdateNeedsRepair(el) {
        if (!birthdateValueLooksAccepted(el)) return true;
        return /正しい日付|有効な日付|invalid date|date is invalid/i.test(fieldContextText(el));
    }

    function fieldContextText(el) {
        var parts = [];
        var node = el;
        for (var depth = 0; node && depth < 5; depth += 1) {
            var text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
            if (text && text.length <= 500) parts.push(text);
            node = node.parentElement;
        }
        return parts.join(' ');
    }

    function uniqueElements(items) {
        var out = [];
        for (var i = 0; i < items.length; i++) {
            if (items[i] && out.indexOf(items[i]) < 0) out.push(items[i]);
        }
        return out;
    }

    // 填写下拉框
    function fillSelect(id, text) {
        var el = document.getElementById(id);
        if (!el) { log('未找到下拉框：' + id); return false; }
        if (!el.options || typeof el.options.length !== 'number') {
            log('字段不是下拉框：' + id);
            return false;
        }
        for (var i = 0; i < el.options.length; i++) {
            if (el.options[i].text.toLowerCase().includes(text.toLowerCase()) || el.options[i].value.toLowerCase().includes(text.toLowerCase())) {
                el.value = el.options[i].value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                log('已选择下拉框：' + id + ' = ' + el.options[i].text);
                return true;
            }
        }
        log('下拉框未匹配选项：' + id + ' / ' + text);
        return false;
    }

    function selectGoogleAddressFirst(inputSel, cb, retries) {
        retries = retries || 0;
        var input = document.querySelector(inputSel);
        if (!input) { log('未找到地址输入框：' + inputSel); cb && cb(false); return; }

        var first = document.querySelector('#suggestedAddressList button') ||
                    document.querySelector('#addressSuggestionContainer button') ||
                    document.querySelector('.pac-container .pac-item');
        if (first) {
            first.click();
            log('已点击谷歌地址候选第一条');
            setTimeout(function() { cb && cb(true); }, 500);
            return;
        }

        if (retries === 0) {
            input.focus();
            input.dispatchEvent(new Event('focus', { bubbles: true }));
        }

        if (retries < 12) {
            setTimeout(function() {
                selectGoogleAddressFirst(inputSel, cb, retries + 1);
            }, 250);
            return;
        }

        log('未找到谷歌地址候选，改用键盘回退');
        input.focus();
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, which: 40, bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
        setTimeout(function() { cb && cb(false); }, 300);
    }

    function fillGoogleAddress(line1Sel, citySel, zipSel, stateId, addr, cb) {
        addr = normalizeJpAddress(addr);
        fillSel(line1Sel, addr.street);
        selectGoogleAddressFirst(line1Sel, function() {
            rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, 0);
            setTimeout(function() { rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, 1); }, 400);
            setTimeout(function() { rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, 2); }, 1200);
            setTimeout(function() {
                rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, 3);
                cb && cb();
            }, 2500);
        });
    }

    function rewriteAddressFields(line1Sel, citySel, zipSel, stateId, addr, pass) {
        fillSel(line1Sel, addr.street);
        fillSel(citySel, addr.city);
        fillSel(zipSel, addr.zip);
        fillState(stateId, addr);
        log('当前：已重写日本账单地址 #' + pass + ' ' + JSON.stringify(addr));
    }

    function fillState(stateId, addr) {
        var stateName = addr && addr.state ? addr.state : JP_FALLBACK_ADDRESS.state;
        var stateNative = addr && addr.stateNative ? addr.stateNative : jpPrefectureNative(stateName);
        var stateLocal = addr && addr.stateLocal ? addr.stateLocal : jpPrefectureLocal(stateName);
        var stateCode = addr && addr.stateCode ? addr.stateCode : jpPrefectureCode(stateName);
        if (fillSelect(stateId, stateCode) || fillSelect(stateId, stateName) || fillSelect(stateId, stateNative) || fillSelect(stateId, stateLocal)) {
            return true;
        }
        var el = document.getElementById(stateId);
        if (!el) {
            log('未找到都道府县字段：' + stateId);
            return false;
        }
        fillElement(el, stateName);
        log('已填写都道府县字段：' + stateId + ' = ' + stateName);
        return true;
    }

    function findFirst(selectors) {
        for (var i = 0; i < selectors.length; i++) {
            try {
                var el = document.querySelector(selectors[i]);
                if (el) return el;
            } catch (e) {}
        }
        return null;
    }

    function fieldText(el) {
        if (!el) return '';
        if (el instanceof HTMLSelectElement) {
            var option = el.options[el.selectedIndex];
            return String((el.value || '') + ' ' + (option ? option.text : '')).trim();
        }
        if ('value' in el) return String(el.value || '').trim();
        return String(el.textContent || '').trim();
    }

    function selectStateElement(el, addr) {
        if (!el) return false;
        var stateName = addr.state;
        var stateNative = addr.stateNative || jpPrefectureNative(stateName);
        var stateLocal = addr.stateLocal || jpPrefectureLocal(stateName);
        var stateCode = addr.stateCode;
        if (el instanceof HTMLSelectElement) {
            for (var i = 0; i < el.options.length; i++) {
                var option = el.options[i];
                var value = String(option.value || '').trim();
                var text = String(option.text || '').trim();
                if (value.toUpperCase() === stateCode.toUpperCase() ||
                    text.toLowerCase() === stateName.toLowerCase() ||
                    text.toLowerCase() === stateNative.toLowerCase() ||
                    text.toLowerCase() === stateLocal.toLowerCase() ||
                    value.toLowerCase() === stateName.toLowerCase()) {
                    el.value = option.value;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                    log('已选择都道府县字段：' + text);
                    return true;
                }
            }
            return false;
        }
        if ('value' in el) {
            fillElement(el, stateName);
            return true;
        }
        el.click();
        setTimeout(function() {
            clickStateOption(stateName, stateCode, stateNative, stateLocal);
        }, 150);
        return true;
    }

    function clickStateOption(stateName, stateCode, stateNative, stateLocal) {
        var nodes = Array.prototype.slice.call(document.querySelectorAll('[role="option"], li, button, div'));
        for (var i = 0; i < nodes.length; i++) {
            var node = nodes[i];
            if (node.offsetParent === null) continue;
            var text = String(node.textContent || '').trim();
            if (!text) continue;
            if (text.toLowerCase() === stateName.toLowerCase() ||
                text.toLowerCase() === String(stateNative || '').toLowerCase() ||
                text.toLowerCase() === String(stateLocal || '').toLowerCase() ||
                text.toUpperCase() === stateCode.toUpperCase()) {
                node.click();
                log('已点击都道府县候选：' + text);
                return true;
            }
        }
        return false;
    }

    function paypalAddressFields() {
        return {
            line1: findFirst([
                '#billingLine1',
                'input[name="billingLine1"]',
                'input[autocomplete="address-line1"]',
                'input[aria-label*="street" i]',
                'input[placeholder*="street" i]'
            ]),
            city: findFirst([
                '#billingCity',
                'input[name="billingCity"]',
                'input[autocomplete="address-level2"]',
                'input[aria-label*="city" i]',
                'input[placeholder*="city" i]'
            ]),
            state: findFirst([
                '#billingState',
                'select[name="billingState"]',
                'input[name="billingState"]',
                '[aria-label*="state" i]',
                '[placeholder*="state" i]'
            ]),
            zip: findFirst([
                '#billingPostalCode',
                'input[name="billingPostalCode"]',
                'input[autocomplete="postal-code"]',
                'input[aria-label*="zip" i]',
                'input[aria-label*="postal" i]',
                'input[placeholder*="zip" i]',
                'input[placeholder*="postal" i]'
            ])
        };
    }

    function isStateValue(addr, value) {
        var raw = String(value || '').trim().toLowerCase();
        if (!raw) return false;
        return raw.indexOf(String(addr.state || '').toLowerCase()) !== -1 ||
               raw.indexOf(String(addr.stateNative || '').toLowerCase()) !== -1 ||
               raw.indexOf(String(addr.stateLocal || '').toLowerCase()) !== -1 ||
               raw.split(/\s+/).indexOf(String(addr.stateCode || '').toLowerCase()) !== -1 ||
               raw === String(addr.stateCode || '').toLowerCase();
    }

    function isPayPalBillingAddressValid() {
        var addr = normalizeJpAddress();
        var fields = paypalAddressFields();
        if (!fields.line1 || !fields.city || !fields.zip || !fields.state) return false;
        return fieldText(fields.line1).toLowerCase().indexOf(addr.street.toLowerCase()) !== -1 &&
               fieldText(fields.city).toLowerCase() === addr.city.toLowerCase() &&
               fieldText(fields.zip) === addr.zip &&
               isStateValue(addr, fieldText(fields.state));
    }

    function isPayPalIdentityValid() {
        if (!currentHost().includes('paypal.com') || !currentPath().includes('/checkoutweb/')) {
            return true;
        }
        var required = paypalRequiredIdentityFields();
        for (var i = 0; i < required.length; i++) {
            var el = required[i];
            if (!fieldText(el) || hasFieldError(el)) return false;
        }
        var birth = birthdateFields();
        for (var j = 0; j < birth.length; j++) {
            if (birthdateNeedsRepair(birth[j])) return false;
        }
        return true;
    }

    function paypalRequiredIdentityFields() {
        var fields = japaneseNameFields().concat(birthdateFields());
        return uniqueElements(fields).filter(function(el) {
            if (!isVisibleElement(el)) return false;
            if (el.required || el.getAttribute('aria-required') === 'true') return true;
            return hasFieldError(el) || /countrySpecific|dateOfBirth|birth|dob|kana|kanji|firstName|lastName|full-name/i.test(inputMeta(el));
        });
    }

    function hasFieldError(el) {
        if (!el) return false;
        if (el.validity && el.validity.valid === false) return true;
        if (String(el.getAttribute('aria-invalid') || '').toLowerCase() === 'true') return true;
        return /正しい日付|有効な日付|ひらがな.*カタカナ.*使用|カタカナ.*使用|ひらがな.*使用|必須|required|invalid|入力してください/i.test(fieldContextText(el));
    }

    function repairPayPalIdentity(reason) {
        if (!currentHost().includes('paypal.com') || !currentPath().includes('/checkoutweb/')) {
            return false;
        }
        var nameFilled = fillJapaneseNames();
        var birthFilled = fillJapaneseBirthdate();
        if (nameFilled || birthFilled) {
            log('当前：已修复 PayPal 日本姓名/生日 ' + (reason || ''));
            return true;
        }
        return false;
    }

    function repairPayPalBillingAddress(reason) {
        if (!currentHost().includes('paypal.com') || !currentPath().includes('/checkoutweb/')) {
            return false;
        }
        var addr = normalizeJpAddress();
        var fields = paypalAddressFields();
        if (!fields.line1 && !fields.city && !fields.zip && !fields.state) {
            return false;
        }
        if (isPayPalBillingAddressValid()) {
            return true;
        }
        log('当前：检测到账单地址不合法，自动修复 ' + (reason || ''));
        if (fields.line1) fillElement(fields.line1, addr.street);
        if (fields.city) fillElement(fields.city, addr.city);
        if (fields.zip) fillElement(fields.zip, addr.zip);
        if (fields.state) selectStateElement(fields.state, addr);
        return true;
    }

    function startPayPalAddressWatcher() {
        if (!currentHost().includes('paypal.com')) {
            return;
        }
        var ticks = 0;
        setInterval(function() {
            ticks += 1;
            if (ticks > 120) return;
            repairPayPalIdentity('watcher #' + ticks);
            repairPayPalBillingAddress('watcher #' + ticks);
        }, 1000);
        setTimeout(function() { repairPayPalIdentity('startup'); }, 500);
        setTimeout(function() { repairPayPalIdentity('startup-late'); }, 2500);
        setTimeout(function() { repairPayPalBillingAddress('startup'); }, 500);
        setTimeout(function() { repairPayPalBillingAddress('startup-late'); }, 2500);
    }

    function httpRequest(opts) {
        if (typeof GM_xmlhttpRequest === 'function') {
            GM_xmlhttpRequest(opts);
            return;
        }
        if (typeof GM !== 'undefined' && GM && typeof GM.xmlHttpRequest === 'function') {
            GM.xmlHttpRequest(opts);
            return;
        }
        if (typeof fetch === 'function') {
            fetch(opts.url, {
                method: opts.method || 'GET',
                headers: opts.headers || {},
                body: opts.data
            }).then(function(res) {
                return res.text().then(function(text) {
                    if (opts.onload) {
                        opts.onload({
                            status: res.status,
                            statusText: res.statusText,
                            responseText: text
                        });
                    }
                });
            }).catch(function(err) {
                if (opts.onerror) {
                    opts.onerror({ statusText: err && err.message ? err.message : 'fetch failed' });
                }
            });
            return;
        }
        throw new Error('No supported HTTP request API available');
    }

    function getValue(id) {
        var el = document.getElementById(id);
        return el ? String(el.value || '').trim() : '';
    }

    function pageText() {
        return document.body ? String(document.body.innerText || '') : '';
    }

    function isPayPalSecurityFlowPage() {
        if (!currentHost().includes('paypal.com')) {
            return false;
        }
        if (window.location.href.includes('/webapps/hermes')) {
            return true;
        }
        return currentPath().includes('/checkoutweb/signup') && hasSecurityCodePromptContent();
    }

    function hasSecurityCodePromptContent() {
        var dialog = securityCodeDialog();
        if (dialog) {
            return true;
        }
        if (securityPromptTextMatches(pageText()) && otpTargetsIn(document).length >= 6) {
            return true;
        }
        var text = pageText();
        if (securityPromptTextMatches(text) && otpInputsIn(document).length >= 6) {
            return true;
        }
        if (currentPath().includes('/checkoutweb/signup')) {
            return false;
        }
        return securityPromptTextMatches(text) && otpInputsIn(document).length > 0;
    }

    function hasOpenAIBillingAddressForm() {
        return !!(document.getElementById('billingAddressLine1') &&
                  document.getElementById('billingCountry') &&
                  document.getElementById('billingAdministrativeArea'));
    }

    function isOpenAIAddressFilled() {
        return !!(getValue('billingAddressLine1') &&
                  getValue('billingLocality') &&
                  getValue('billingPostalCode') &&
                  getValue('billingAdministrativeArea'));
    }

    function isSignupAddressFilled() {
        if (currentHost().includes('paypal.com')) {
            return isPayPalBillingAddressValid() && isPayPalIdentityValid();
        }
        return !!(getValue('billingLine1') &&
                  getValue('billingCity') &&
                  getValue('billingPostalCode') &&
                  getValue('billingState'));
    }

    function waitForOpenAIAddressFilled(cb, retries) {
        retries = retries || 0;
        if (isOpenAIAddressFilled()) {
            log('已确认 OpenAI 账单地址已填写');
            cb(true);
            return;
        }
        if (retries < 10) {
            setTimeout(function() {
                waitForOpenAIAddressFilled(cb, retries + 1);
            }, 500);
            return;
        }
        log('等待：OpenAI 账单地址尚未填写完整');
        cb(false);
    }

    function waitForSignupAddressFilled(cb, retries) {
        retries = retries || 0;
        if (isSignupAddressFilled()) {
            log('已确认 signup 页面账单地址已填写');
            cb(true);
            return;
        }
        if (retries < 10) {
            setTimeout(function() {
                waitForSignupAddressFilled(cb, retries + 1);
            }, 500);
            return;
        }
        log('等待：signup 页面地址还没确认填写完成');
        cb(false);
    }

    function ensureOpenAIPayPalAddressForm(cb, retries) {
        retries = retries || 0;
        if (hasOpenAIBillingAddressForm()) {
            log('已看到 PayPal 账单地址表单');
            cb(true);
            return;
        }

        var ppBtn = document.querySelector('[data-testid="paypal-accordion-item-button"]') ||
                    document.querySelector('.paypal-accordion-item button');
        if (ppBtn) {
            ppBtn.click();
            log('当前：正在点击 PayPal 按钮，等待账单地址表单出现');
        }

        if (retries < 8) {
            setTimeout(function() {
                ensureOpenAIPayPalAddressForm(cb, retries + 1);
            }, 1000);
            return;
        }

        log('等待结束：PayPal 账单地址表单仍未出现');
        cb(false);
    }

    var cachedSecurityCode = null;

    function getSecurityCode(cb, retries) {
        retries = retries || 0;
        if (cachedSecurityCode) {
            log('当前：复用缓存的短信验证码');
            cb(cachedSecurityCode);
            return;
        }
        if (retries === 0 && smsPollingActive) {
            log('当前：短信接口已有轮询任务，跳过重复启动');
            return;
        }
        smsPollingActive = true;

        ensureCheckoutSmsLease(function(smsConfig) {
            if (!smsConfig) {
                smsPollingActive = false;
                log('未获取到短信号码，无法轮询验证码');
                releaseCheckoutSmsLease('lease-unavailable');
                cb(null);
                return;
            }
            pollSecurityCodeWithSms(smsConfig, cb, retries);
        });
    }

    function pollSecurityCodeWithSms(smsConfig, cb, retries) {
        retries = retries || 0;
        var paypalSmsApiUrl = smsConfig.smsUrl || '';
        if (!paypalSmsApiUrl) {
            smsPollingActive = false;
            log('未配置短信收码 URL，无法自动获取验证码');
            cb(null);
            return;
        }
        log('当前：正在轮询短信接口 ' + maskUrl(paypalSmsApiUrl));
        var smsUrl = paypalSmsApiUrl + (paypalSmsApiUrl.indexOf('?') === -1 ? '?' : '&') + '_ts=' + Date.now();

        httpRequest({
            method: 'GET',
            url: smsUrl,
            headers: {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'accept-language': 'zh,en;q=0.9,zh-CN;q=0.8',
                'cache-control': 'max-age=0, no-cache',
                'pragma': 'no-cache'
            },
            onload: function(r) {
                try {
                    var raw = extractSmsText(r.responseText);
                    var m = String(raw).match(/\b(\d{6})\b/);
                    if (m) {
                        smsPollingActive = false;
                        cachedSecurityCode = m[1];
                        log('已获取短信验证码：' + cachedSecurityCode);
                        cb(cachedSecurityCode);
                        return;
                    }
                    log('短信接口当前没有 6 位验证码：' + maskSmsText(raw));
                } catch (e) {
                    log('短信验证码解析失败：' + e.message);
                }

                if (shouldPollSmsAgain(retries)) {
                    log('等待：' + CONFIG.smsPollSeconds + ' 秒后继续轮询短信验证码 #' + (retries + 1));
                    setTimeout(function() { pollSecurityCodeWithSms(smsConfig, cb, retries + 1); }, CONFIG.smsPollSeconds * 1000);
                    return;
                }
                smsPollingActive = false;
                cb(null);
            },
            onerror: function(e) {
                log('短信验证码请求失败：' + (e.statusText || 'network error'));
                if (shouldPollSmsAgain(retries)) {
                    log('等待：' + CONFIG.smsPollSeconds + ' 秒后继续轮询短信验证码 #' + (retries + 1));
                    setTimeout(function() { pollSecurityCodeWithSms(smsConfig, cb, retries + 1); }, CONFIG.smsPollSeconds * 1000);
                    return;
                }
                smsPollingActive = false;
                cb(null);
            }
        });
    }

    function maskUrl(url) {
        return String(url || '').replace(/([?&](?:token|key|api_key|apikey|phone)=)[^&]+/ig, '$1***');
    }

    function maskSmsText(text) {
        return String(text || '')
            .replace(/\b\d{6}\b/g, '******')
            .replace(/\b\d{10,}\b/g, function(m) { return m.slice(0, 3) + '***' + m.slice(-2); })
            .slice(0, 500);
    }

    function extractSmsText(responseText) {
        var text = String(responseText || '');
        try {
            var d = JSON.parse(text);
            return extractSmsTextFromJson(d) || text;
        } catch (e) {
            return text;
        }
    }

    function extractSmsTextFromJson(value) {
        if (!value) return '';
        if (typeof value === 'string' || typeof value === 'number') {
            var direct = String(value || '').trim();
            return /^(null|undefined|none)$/i.test(direct) ? '' : direct;
        }
        if (Array.isArray(value)) {
            return value.map(extractSmsTextFromJson).filter(Boolean).join('\n');
        }
        if (typeof value === 'object') {
            var keys = [
                'SmsCode', 'smsCode', 'code', 'Code', 'verifyCode', 'verificationCode',
                'SmsContent', 'smsContent', 'content', 'Content', 'sms', 'text', 'message', 'msg', 'data'
            ];
            var parts = [];
            for (var i = 0; i < keys.length; i++) {
                if (Object.prototype.hasOwnProperty.call(value, keys[i])) {
                    var part = extractSmsTextFromJson(value[keys[i]]);
                    if (part) parts.push(part);
                }
            }
            return parts.join('\n');
        }
        return '';
    }

    function shouldPollSmsAgain(retries) {
        var maxRetries = Math.ceil((Number(CONFIG.smsTimeoutSeconds) || 180) / (Number(CONFIG.smsPollSeconds) || 2));
        return retries < Math.max(1, maxRetries);
    }

    function hasSecurityCodePrompt() {
        if (!isPayPalSecurityFlowPage()) {
            return false;
        }
        return hasSecurityCodePromptContent();
    }

    function handleSecurityCodePrompt() {
        if (!currentHost().includes('paypal.com')) {
            return false;
        }
        if (!hasSecurityCodePrompt()) {
            return false;
        }
        if (securityAutoStopped) {
            log('当前：自动验证码输入已停止，请手动输入缓存验证码');
            return true;
        }
        if (securityCodeHandling) {
            if (cachedSecurityCode) {
                retryFillCachedSecurityCode('handling-loop');
            }
            log('当前：短信验证码正在自动处理中');
            return true;
        }
        securityCodeHandling = true;
        log('当前：已进入短信验证码页面，准备自动获取并填写验证码');
        getSecurityCode(function(code) {
            if (!code) {
                securityCodeHandling = false;
                log('未能自动获取短信验证码，请手动填写验证码');
                waitForSecurityCodeDismiss();
                return;
            }
            if (!fillSecurityCode(code)) {
                securityCodeHandling = false;
                log('验证码输入框未找到，请手动填写验证码：' + code);
                waitForSecurityCodeDismiss();
                return;
            }
            securityFillAttempts += 1;
            lastSecurityFillAt = Date.now();
            setTimeout(function() {
                clickSecurityCodeSubmitButtonIfReady();
                waitForSecurityCodeDismiss();
            }, 1200);
        });
        return true;
    }

    function retryFillCachedSecurityCode(reason) {
        if (!cachedSecurityCode || !hasSecurityCodePrompt()) {
            return false;
        }
        if (securityAutoStopped) {
            return false;
        }
        if (securityFillAttempts >= 3 && !isSecurityCodeVisiblyFilled()) {
            securityAutoStopped = true;
            securityCodeHandling = false;
            log('当前：自动输入验证码连续失败，已停止自动重试。请手动输入验证码：' + cachedSecurityCode);
            return false;
        }
        var now = Date.now();
        if (now - lastSecurityFillAt < 1200) {
            return false;
        }
        securityFillAttempts += 1;
        lastSecurityFillAt = now;
        log('当前：使用缓存验证码重试填入 #' + securityFillAttempts + ' reason=' + reason);
        if (!fillSecurityCode(cachedSecurityCode)) {
            log('缓存验证码重填失败：未找到输入框');
            return false;
        }
        setTimeout(function() {
            clickSecurityCodeSubmitButtonIfReady();
        }, 1200);
        return true;
    }

    function startSecurityCodeWatcher() {
        if (!currentHost().includes('paypal.com')) {
            return;
        }
        log('当前：短信验证码 watcher 已启动');
        setInterval(function() {
            securityWatcherTicks += 1;
            if (securityWatcherTicks % 5 === 0) {
                log('心跳：验证码弹窗=' + hasSecurityCodePromptContent() + ' 处理中=' + securityCodeHandling);
            } else {
                renderDebugPanel();
            }
            if (cachedSecurityCode && hasSecurityCodePromptContent()) {
                retryFillCachedSecurityCode('watcher');
            }
            handleSecurityCodePrompt();
        }, 1000);
        setTimeout(function() {
            handleSecurityCodePrompt();
        }, 250);
    }

    function waitForSecurityCodeDismiss(retries) {
        retries = retries || 0;
        if (!hasSecurityCodePrompt()) {
            securityCodeHandling = false;
            log('当前：验证码弹窗已关闭，继续后续流程');
            releaseCheckoutSmsLease('security-dismissed');
            setTimeout(function() { clickBtnWithRetry(); }, 1000);
            return;
        }
        if (retries < 120) {
            setTimeout(function() { waitForSecurityCodeDismiss(retries + 1); }, 1000);
            return;
        }
        log('等待结束：验证码弹窗长时间未关闭');
    }

    function clickSecurityCodeSubmitButton() {
        var root = securityCodeDialog() || document;
        var buttons = Array.prototype.slice.call(root.querySelectorAll('button')).filter(function(btn) {
            return btn.offsetParent !== null && !btn.disabled;
        });
        for (var i = 0; i < buttons.length; i++) {
            var text = String(buttons[i].textContent || '').trim();
            if (/^(continue|submit|verify|confirm|next|done|继续|提交|验证|确认|下一步|続行|送信|確認|次へ|完了)$/i.test(text)) {
                log('当前：正在点击验证码提交按钮 ' + text);
                buttons[i].click();
                return true;
            }
        }
        var active = document.activeElement;
        if (active) {
            active.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
            active.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
            log('当前：未找到验证码提交按钮，已发送 Enter');
            return true;
        }
        return clickBtnWithRetry();
    }

    function clickSecurityCodeSubmitButtonIfReady() {
        if (!isSecurityCodeVisiblyFilled()) {
            log('当前：验证码尚未写入页面，暂不提交');
            return false;
        }
        return clickSecurityCodeSubmitButton();
    }

    function isSecurityCodeVisiblyFilled() {
        var inputs = securityCodeInputs();
        if (inputs.length === 1 && isWritableOtpElement(inputs[0])) {
            return /^\d{6}$/.test(String(inputs[0].value || '').trim());
        }
        var active = document.activeElement;
        if (isWritableOtpElement(active) && /^\d{6}$/.test(readOtpElementValue(active))) {
            return true;
        }
        var writableInputs = inputs.filter(isWritableOtpElement);
        if (writableInputs.length >= 6) {
            var code = writableInputs.slice(0, 6).map(function(el) {
                return readOtpElementValue(el);
            }).join('');
            return /^\d{6}$/.test(code);
        }
        if (writableInputs.length === 1) {
            return /^\d{6}$/.test(readOtpElementValue(writableInputs[0]));
        }
        var root = securityCodeDialog() || document;
        var text = String(root.innerText || root.textContent || '');
        if (text) {
            var visibleDigits = text.match(/\b\d\s*\d\s*\d\s*\d\s*\d\s*\d\b/);
            if (visibleDigits && /^\d{6}$/.test(visibleDigits[0].replace(/\D/g, ''))) {
                return true;
            }
        }
        return false;
    }

    function securityCodeInputs() {
        var root = securityCodeDialog();
        if (!root) {
            if (securityPromptTextMatches(pageText())) {
                var globalInputs = otpTargetsIn(document);
                if (globalInputs.length) {
                    log('当前：未找到验证码弹窗容器，使用页面全局 OTP 输入框');
                    return globalInputs;
                }
            }
            log('未找到验证码弹窗容器');
            return [];
        }
        return otpTargetsIn(root);
    }

    function otpInputsIn(root) {
        var inputs = queryOtpCandidates(root).filter(function(el) {
            return !isDisabledOtpElement(el) && !isPhoneOrCountryInput(el);
        });
        var single = inputs.filter(function(el) {
            var id = inputMeta(el);
            return /otp|security|verify|one-time|verification/i.test(id) ||
                   el.autocomplete === 'one-time-code' ||
                   (el.maxLength && el.maxLength === 6 && !isPhoneOrCountryInput(el));
        });
        if (single.length) return [single[0]];
        var digitBoxes = inputs.filter(function(el) {
            var rect = el.getBoundingClientRect();
            return isVisibleElement(el) &&
                   rect.width >= 20 && rect.width <= 90 &&
                   rect.height >= 20 && rect.height <= 90 &&
                   !isPhoneOrCountryInput(el);
        });
        if (digitBoxes.length >= 6 && securityPromptTextMatches(rootText(root))) {
            return digitBoxes.slice(0, 6).sort(inputPositionSort);
        }
        return inputs.filter(function(el) {
            var rect = el.getBoundingClientRect();
            var id = el.id || '';
            var name = el.name || '';
            var meta = inputMeta(el);
            return (isVisibleElement(el) && el.maxLength === 1 && /otp|code|security|verify|one-time|verification/i.test(meta)) ||
                   /^ci-ciBasic-\d+$/.test(id) ||
                   /^ciBasic-\d+$/.test(name) ||
                   /^\d-6$/.test(el.getAttribute('aria-label') || '');
        }).sort(inputPositionSort);
    }

    function otpTargetsIn(root) {
        var inputs = otpInputsIn(root);
        if (inputs.length) return inputs;
        if (!securityPromptTextMatches(rootText(root))) return [];
        return queryVisibleOtpBoxes(root).slice(0, 6);
    }

    function queryVisibleOtpBoxes(root) {
        var boxes = [];
        var seen = [];
        function scan(scope) {
            if (!scope || seen.indexOf(scope) >= 0) return;
            seen.push(scope);
            try {
                Array.prototype.forEach.call(scope.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"], div, span'), function(el) {
                    if (el === debugPanelNode || !isVisibleElement(el) || isPhoneOrCountryInput(el)) return;
                    var rect = el.getBoundingClientRect();
                    if (rect.width >= 24 && rect.width <= 72 && rect.height >= 32 && rect.height <= 72) {
                        boxes.push(el);
                    }
                });
                Array.prototype.forEach.call(scope.querySelectorAll('*'), function(el) {
                    if (el.shadowRoot) scan(el.shadowRoot);
                });
            } catch (e) {}
        }
        scan(root || document);
        return boxes.sort(inputPositionSort).filter(function(el, index, arr) {
            var rect = el.getBoundingClientRect();
            for (var i = 0; i < index; i++) {
                var prev = arr[i].getBoundingClientRect();
                if (Math.abs(prev.left - rect.left) < 3 && Math.abs(prev.top - rect.top) < 3) {
                    return false;
                }
            }
            return true;
        });
    }

    function queryOtpCandidates(root) {
        var found = [];
        var seen = [];
        function addAll(scope) {
            if (!scope || seen.indexOf(scope) >= 0) return;
            seen.push(scope);
            try {
                Array.prototype.forEach.call(scope.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]'), function(el) {
                    if (found.indexOf(el) < 0) found.push(el);
                });
                Array.prototype.forEach.call(scope.querySelectorAll('*'), function(el) {
                    if (el.matches && el.matches('iframe')) {
                        try { addAll(el.contentDocument); } catch (_) {}
                    }
                    if (el.shadowRoot) addAll(el.shadowRoot);
                });
            } catch (e) {}
        }
        addAll(root || document);
        return found;
    }

    function securityCodeDialog() {
        var candidates = Array.prototype.slice.call(document.querySelectorAll(
            '[role="dialog"], [aria-modal="true"], .modal, .vx_modal, div'
        )).filter(function(el) {
            if (el === debugPanelNode || !el || !isVisibleElement(el)) return false;
            var text = String(el.innerText || '');
            if (!securityPromptTextMatches(text)) return false;
            return el.querySelectorAll && otpTargetsIn(el).length > 0;
        }).sort(function(a, b) {
            var ar = a.getBoundingClientRect();
            var br = b.getBoundingClientRect();
            var aa = Math.max(1, ar.width * ar.height);
            var ba = Math.max(1, br.width * br.height);
            return aa - ba;
        });
        if (candidates.length) {
            return candidates[0];
        }
        return null;
    }

    function isVisibleElement(el) {
        if (!el || !el.getBoundingClientRect) {
            return false;
        }
        var rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) {
            return false;
        }
        var style = window.getComputedStyle ? window.getComputedStyle(el) : null;
        if (style && (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity || 1) === 0)) {
            return false;
        }
        return true;
    }

    function securityPromptTextMatches(text) {
        return /enter your code|we sent a 6-digit code|6-digit code to|resend|verification code|security code|コードを入力|6桁のコード|6\s*桁|セキュリティコード|確認コード|認証コード|再送|送信しました/i.test(String(text || ''));
    }

    function rootText(root) {
        if (!root) return '';
        if (root === document) return pageText();
        return String(root.innerText || root.textContent || '');
    }

    function inputPositionSort(a, b) {
        var ar = a.getBoundingClientRect();
        var br = b.getBoundingClientRect();
        if (Math.abs(ar.top - br.top) > 8) return ar.top - br.top;
        if (Math.abs(ar.left - br.left) > 4) return ar.left - br.left;
        return 0;
    }

    function inputMeta(el) {
        if (!el || !el.getAttribute) return '';
        return [
            el.id || '',
            el.name || '',
            el.autocomplete || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('data-testid') || '',
            el.getAttribute('role') || '',
            el.placeholder || '',
            el.type || ''
        ].join(' ');
    }

    function isPhoneOrCountryInput(el) {
        var meta = inputMeta(el);
        if (isOtpLikeInput(el)) {
            return false;
        }
        return /phone|mobile|country|calling|dial|prefix|codePhone|countryCode|phoneCode/i.test(meta) ||
               el.id === 'phone' ||
               el.name === 'phone';
    }

    function isDisabledOtpElement(el) {
        return !el || el.disabled || el.readOnly || el.getAttribute('aria-disabled') === 'true';
    }

    function isOtpLikeInput(el) {
        var meta = inputMeta(el);
        return /otp|security|verify|verification|one-time|one time|auth|ciBasic|ci-ciBasic/i.test(meta) ||
               el.autocomplete === 'one-time-code' ||
               (el.maxLength === 1 && /numeric|decimal|tel/i.test(String(el.getAttribute('inputmode') || el.type || ''))) ||
               (el.maxLength === 6 && /numeric|decimal|tel|text|password/i.test(String(el.getAttribute('inputmode') || el.type || '')));
    }

    function isWritableOtpElement(el) {
        if (!el || isDisabledOtpElement(el)) return false;
        if ('value' in el && /^(input|textarea|select)$/i.test(el.tagName || '')) return true;
        if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') return true;
        return (el.getAttribute('role') || '').toLowerCase() === 'textbox';
    }

    function readOtpElementValue(el) {
        if (!el) return '';
        if ('value' in el) return String(el.value || '').trim();
        return String(el.textContent || '').replace(/\D/g, '').trim();
    }

    function fillSecurityCode(code) {
        var inputs = securityCodeInputs();
        log('当前：可见验证码候选输入框数量 ' + inputs.length);

        if (inputs.length === 1) {
            typeOtpCode([inputs[0]], code);
            log('已填写短信验证码：单输入框');
            return true;
        }

        if (inputs.length >= 6) {
            typeOtpCode(inputs.slice(0, 6), code);
            log('已填写短信验证码：分割输入框，数量=' + inputs.length);
            return true;
        }

        log('未找到短信验证码输入框，候选数量：' + inputs.length);
        return false;
    }

    function typeOtpCode(inputs, code) {
        var digits = String(code || '').split('');
        if (!inputs.length || !digits.length) {
            return;
        }
        var writableInputs = inputs.filter(isWritableOtpElement);
        if (inputs.length === 1) {
            var singleTarget = resolveOtpWritableTarget(inputs[0]);
            if (singleTarget) {
                log('当前：验证码真实输入目标 ' + elementDebugName(singleTarget));
                writeWholeOtpCode(singleTarget, String(code || ''));
                return;
            }
            clickOtpBoxAndType(inputs[0], String(code || ''));
            return;
        }
        if (writableInputs.length === 1) {
            log('当前：验证码真实输入目标 ' + elementDebugName(writableInputs[0]));
            writeWholeOtpCode(writableInputs[0], String(code || ''));
            return;
        }
        if (writableInputs.length >= 6) {
            log('当前：开始逐位写入真实验证码输入框');
            writeSplitOtpCode(writableInputs.slice(0, 6), digits);
            return;
        }
        var resolvedList = resolveOtpWritableTargets(inputs[0]);
        if (resolvedList.length >= 6) {
            log('当前：从可见格解析到 6 个真实输入框，开始逐位写入');
            writeSplitOtpCode(resolvedList.slice(0, 6), digits);
            return;
        }
        var resolved = resolveOtpWritableTarget(inputs[0]);
        if (resolved) {
            log('当前：点击可见格后解析到真实输入目标 ' + elementDebugName(resolved));
            writeWholeOtpCode(resolved, String(code || ''));
            return;
        }
        if (inputs.length >= 6) {
            clickOtpBoxAndType(inputs[0], String(code || ''));
            return;
        }
        log('当前：开始逐位写入验证码');
        focusOtpInput(inputs[0]);
        digits.slice(0, inputs.length).forEach(function(ch, index) {
            setTimeout(function() {
                var target = inputs[index];
                fillOtpInput(target, ch);
                var next = inputs[index + 1];
                if (next) focusOtpInput(next);
                log('当前：已写入验证码位 #' + (index + 1));
            }, index * 80);
        });
    }

    function writeSplitOtpCode(targets, digits) {
        focusOtpInput(targets[0]);
        digits.slice(0, targets.length).forEach(function(ch, index) {
            setTimeout(function() {
                var target = targets[index];
                fillOtpInput(target, ch);
                var next = targets[index + 1];
                if (next) focusOtpInput(next);
                log('当前：已写入验证码真实输入位 #' + (index + 1));
            }, index * 80);
        });
    }

    function writeWholeOtpCode(target, code) {
        focusOtpInput(target);
        pasteOtpCode(target, code);
        if (isWritableOtpElement(target)) {
            fillOtpInput(target, code);
        }
        insertOtpTextByCommand(code);
        sendOtpKeysToFocused(code);
    }

    function clickOtpBoxAndType(box, code) {
        focusOtpInput(box);
        var active = deepActiveElement();
        log('当前：点击验证码格后焦点 ' + elementDebugName(active));
        var writable = isWritableOtpElement(active) ? active : resolveOtpWritableTarget(box);
        if (writable) {
            log('当前：使用焦点/邻近真实输入目标 ' + elementDebugName(writable));
            writeWholeOtpCode(writable, code);
            return;
        }
        pasteOtpCode(box, code);
        sendOtpKeysToFocused(code);
    }

    function sendOtpKeysToFocused(code) {
        String(code || '').split('').slice(0, 6).forEach(function(ch, index) {
            setTimeout(function() {
                var active = deepActiveElement();
                var target = active && active !== document.body ? active : document;
                dispatchKey(target, 'keydown', ch);
                dispatchKey(target, 'keypress', ch);
                dispatchBeforeInput(target, ch);
                if (isWritableOtpElement(active)) {
                    fillOtpInput(active, appendOtpDigit(readOtpElementValue(active), ch));
                } else {
                    dispatchInput(target, ch);
                }
                dispatchKey(target, 'keyup', ch);
                log('当前：已向当前焦点发送验证码位 #' + (index + 1) + ' focus=' + elementDebugName(active));
            }, index * 120);
        });
    }

    function appendOtpDigit(current, digit) {
        var value = String(current || '').replace(/\D/g, '');
        if (value.length >= 6) return value;
        return value + String(digit || '').replace(/\D/g, '').slice(0, 1);
    }

    function dispatchBeforeInput(el, value) {
        try {
            el.dispatchEvent(new InputEvent('beforeinput', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertText',
                data: value
            }));
        } catch (e) {}
    }

    function dispatchInput(el, value) {
        try {
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: 'insertText',
                data: value
            }));
        } catch (e) {
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function fillOtpInput(el, val) {
        var value = String(val == null ? '' : val);
        focusOtpInput(el);
        dispatchKey(el, 'keydown', value.slice(-1) || value);
        dispatchKey(el, 'keypress', value.slice(-1) || value);
        if ('value' in el) {
            dispatchBeforeInput(el, value);
            fillElement(el, value);
            dispatchInput(el, value);
        } else if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') {
            dispatchBeforeInput(el, value);
            el.textContent = value;
            dispatchInput(el, value);
        } else {
            var active = document.activeElement && document.activeElement !== document.body ? document.activeElement : el;
            dispatchBeforeInput(active, value);
            dispatchInput(active, value);
        }
        dispatchKey(el, 'keyup', value.slice(-1) || value);
    }

    function pasteOtpCode(target, code) {
        var value = String(code || '');
        focusOtpInput(target);
        var active = deepActiveElement();
        if (!active || active === document.body) active = target;
        var root = securityCodeDialog() || active;
        log('当前：尝试 paste 整串验证码到 ' + elementDebugName(active));
        dispatchPaste(active, value);
        if (root !== active) dispatchPaste(root, value);
        dispatchBeforeInput(active, value);
        dispatchInput(active, value);
    }

    function insertOtpTextByCommand(code) {
        try {
            if (document.queryCommandSupported && document.queryCommandSupported('insertText')) {
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, String(code || ''));
                log('当前：已尝试 execCommand 写入验证码');
            }
        } catch (e) {}
    }

    function resolveOtpWritableTarget(seed) {
        if (isWritableOtpElement(seed)) return seed;
        focusOtpInput(seed);
        var active = deepActiveElement();
        log('当前：解析验证码输入目标，点击后焦点 ' + elementDebugName(active));
        if (isWritableOtpElement(active) && !isPhoneOrCountryInput(active)) return active;
        var near = findWritableOtpNear(seed);
        if (near) return near;
        var all = resolveOtpWritableTargets(seed);
        if (all.length === 1) return all[0];
        if (all.length >= 6) return all[0];
        return null;
    }

    function resolveOtpWritableTargets(seed) {
        var root = securityCodeDialog() || rootNodeOf(seed) || document;
        return queryOtpCandidates(root).filter(function(el) {
            return isWritableOtpElement(el) && !isPhoneOrCountryInput(el);
        }).sort(inputPositionSort);
    }

    function findWritableOtpNear(seed) {
        var current = seed;
        for (var depth = 0; current && depth < 6; depth += 1) {
            var found = firstWritableOtpIn(current);
            if (found) return found;
            current = current.parentElement || current.parentNode;
        }
        var root = rootNodeOf(seed);
        if (root && root !== document) {
            return firstWritableOtpIn(root);
        }
        return null;
    }

    function firstWritableOtpIn(root) {
        var found = queryOtpCandidates(root).filter(function(el) {
            return isWritableOtpElement(el) && !isPhoneOrCountryInput(el);
        }).sort(inputPositionSort);
        return found[0] || null;
    }

    function deepActiveElement() {
        var active = document.activeElement;
        var guard = 0;
        while (active && active.shadowRoot && active.shadowRoot.activeElement && guard < 8) {
            active = active.shadowRoot.activeElement;
            guard += 1;
        }
        try {
            if (active && active.tagName === 'IFRAME' && active.contentDocument && active.contentDocument.activeElement) {
                active = active.contentDocument.activeElement;
            }
        } catch (e) {}
        return active;
    }

    function rootNodeOf(el) {
        try {
            return el && el.getRootNode ? el.getRootNode() : null;
        } catch (e) {
            return null;
        }
    }

    function dispatchPaste(el, text) {
        try {
            var dt = new DataTransfer();
            dt.setData('text/plain', text);
            el.dispatchEvent(new ClipboardEvent('paste', {
                bubbles: true,
                cancelable: true,
                clipboardData: dt
            }));
        } catch (e) {
            try {
                el.dispatchEvent(new Event('paste', { bubbles: true, cancelable: true }));
            } catch (_) {}
        }
    }

    function elementDebugName(el) {
        if (!el) return 'null';
        return String((el.tagName || 'node') + '#' + (el.id || '') + '.' + (el.className || '')).slice(0, 120);
    }

    function focusOtpInput(el) {
        try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
        try { el.focus({ preventScroll: true }); } catch (e) {
            try { el.focus(); } catch (_) {}
        }
        try { el.click(); } catch (e) {}
    }

    function dispatchKey(el, type, key) {
        var code = /^\d$/.test(key) ? 48 + Number(key) : 0;
        try {
            el.dispatchEvent(new KeyboardEvent(type, {
                key: key,
                code: /^\d$/.test(key) ? 'Digit' + key : key,
                keyCode: code,
                which: code,
                bubbles: true,
                cancelable: true
            }));
        } catch (e) {}
    }

    function clickConsentButton(retries) {
        retries = retries || 0;
        var btn = document.getElementById('consentButton') ||
                  document.querySelector('button[data-testid="consentButton"]');
        if (btn) {
            if (btn.disabled) {
                log('等待：同意按钮暂时不可用');
                if (retries < 20) setTimeout(function() { clickConsentButton(retries + 1); }, 500);
                return;
            }
            log('当前：正在点击同意按钮');
            btn.click();
            return;
        }
        if (retries < 20) {
            setTimeout(function() { clickConsentButton(retries + 1); }, 500);
            return;
        }
        log('未找到同意按钮');
    }

    function fallbackProfile() {
        return {
            address: normalizeJpAddress(JP_FALLBACK_ADDRESS),
            card: {
                number: CONFIG.cardNumber,
                expiry: CONFIG.cardExpiry,
                cvv: CONFIG.cardCvv
            }
        };
    }

    // 从meiguodizhi.com API获取地址和卡信息
    function getProfile(cb) {
        if (cachedProfile) {
            log('当前：复用缓存的地址和卡信息');
            return cb(cachedProfile);
        }
        log('当前：正在从 meiguodizhi.com API 获取地址和卡信息');
        httpRequest({
            method: 'POST',
            url: 'https://www.meiguodizhi.com/api/v1/dz',
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ path: '/', method: 'address' }),
            onload: function(r) {
                try {
                    var d = JSON.parse(r.responseText);
                    var a = d.address || d;
                    var profile = {
                        address: normalizeJpAddress(a),
                        card: {
                            number: a.Credit_Card_Number || d.Credit_Card_Number || CONFIG.cardNumber,
                            expiry: a.Expires || a.Expiry || d.Expires || CONFIG.cardExpiry,
                            cvv: a.CVV2 || a.CVV || d.CVV2 || CONFIG.cardCvv
                        }
                    };
                    cachedProfile = profile;
                    log('已从 API 获取资料：' + JSON.stringify(profile));
                    cb(profile);
                } catch(e) {
                    log('资料解析失败：' + e.message);
                    cachedProfile = fallbackProfile();
                    cb(cachedProfile);
                }
            },
            onerror: function(e) {
                log('资料请求失败：' + (e.statusText || 'network error'));
                cachedProfile = fallbackProfile();
                cb(cachedProfile);
            }
        });
    }

    // 点击按钮（带重试）
    function clickBtn(retries) {
        retries = retries || 0;
        var btn = document.querySelector('button[data-testid="submit-button"]') ||
                  document.querySelector('button[data-testid="hosted-payment-submit-button"]') ||
                  document.querySelector('button[data-atomic-wait-intent="Submit_Email"]') ||
                  document.querySelector('button.SubmitButton--complete');
        if (!btn) {
            var all = document.querySelectorAll('button');
            for (var i = 0; i < all.length; i++) {
                var t = all[i].textContent.trim();
                if (t === '下一页' || t === 'Next' || t === 'Subscribe' || t === 'Pay' || t === 'Continue' || t === 'Agree' || t === 'Create an Account' || t === 'Create Account' || t === 'Agree & Create Account' || t === 'Agree and Create Account' || t === 'Agree & Continue') {
                    btn = all[i]; break;
                }
            }
        }
        if (btn) {
            if (btn.disabled) {
                log('等待：提交按钮暂时不可用');
                if (retries < 10) setTimeout(function() { clickBtn(retries + 1); }, 1000);
                return false;
            }
            var rect = btn.getBoundingClientRect();
            log('已找到按钮：' + btn.textContent.trim() + '，可见：' + (rect.height > 0));
            if (rect.height === 0) {
                log('等待：按钮当前不可见，准备重试');
                if (retries < 10) setTimeout(function() { clickBtn(retries + 1); }, 1000);
                return false;
            }
            log('当前：正在点击 ' + btn.textContent.trim());
            btn.click();
            return true;
        } else {
            log('等待：未找到提交按钮，准备重试（' + retries + '）');
            if (retries < 10) setTimeout(function() { clickBtn(retries + 1); }, 1000);
            return false;
        }
    }

    function findButtonByTexts(texts) {
        var buttons = document.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
            var text = buttons[i].textContent.trim();
            for (var j = 0; j < texts.length; j++) {
                if (text === texts[j]) {
                    return buttons[i];
                }
            }
        }
        return null;
    }

    function clickElement(el, label) {
        if (!el) {
            return false;
        }
        var text = (label || el.textContent || '').trim();
        var rect = el.getBoundingClientRect();
        if (rect.height === 0) {
            log('等待：按钮不可见 ' + text);
            return false;
        }
        if (el.disabled) {
            log('等待：按钮不可用 ' + text);
            return false;
        }
        if (/creating|processing|loading|正在处理|创建中/i.test(text)) {
            log('等待：按钮处于处理中状态 ' + text);
            return false;
        }
        log('当前：正在点击 ' + text);
        el.click();
        return true;
    }

    function handlePayEntryFlow(retries) {
        retries = retries || 0;

        if (currentPath().includes('/checkoutweb/signup')) {
            log('当前：已进入 signup 页面');
            return true;
        }

        if (handleSecurityCodePrompt()) {
            return true;
        }

        var createBtn = document.querySelector('button[data-atomic-wait-intent="Pay_With_Card"]') ||
                        findButtonByTexts(['Create an Account', 'Create Account']);
        if (createBtn) {
            if (clickElement(createBtn, createBtn.textContent.trim() || 'Create an Account')) {
                if (retries < 12) {
                    setTimeout(function() { handlePayEntryFlow(retries + 1); }, 1500);
                }
                return true;
            }
        }

        var continueBtn = document.querySelector('button[data-testid="continueButton"]') ||
                          findButtonByTexts(['Continue to Payment']);
        var emailInput = document.getElementById('login_email') ||
                         document.getElementById('email') ||
                         document.querySelector('input[type="email"]');
        if (continueBtn) {
            if (emailInput && !String(emailInput.value || '').trim()) {
                var email = randEmail();
                fillElement(emailInput, email);
                log('当前：已填写 PayPal 入口邮箱 ' + email);
            }
            if (clickElement(continueBtn, continueBtn.textContent.trim() || 'Continue to Payment')) {
                if (retries < 12) {
                    setTimeout(function() { handlePayEntryFlow(retries + 1); }, 2000);
                }
                return true;
            }
        }

        if (retries < 12) {
            log('等待：/pay 页面下一步按钮或邮箱输入框尚未就绪');
            setTimeout(function() { handlePayEntryFlow(retries + 1); }, 1500);
            return true;
        }

        log('结束：/pay 页面流程未能继续推进');
        return false;
    }

    function clickBtnWithRetry(originUrl, retries) {
        retries = retries || 0;
        if (currentHost().includes('paypal.com') && /^\/pay\/?$/.test(currentPath())) {
            return handlePayEntryFlow(retries);
        }
        repairPayPalIdentity('before submit');
        repairPayPalBillingAddress('before submit');
        if (currentHost().includes('paypal.com') &&
            currentPath().includes('/checkoutweb/signup') &&
            !isPayPalIdentityValid() &&
            retries < 8) {
            log('等待：日本姓名/生日仍未通过页面校验，暂不提交');
            setTimeout(function() { clickBtnWithRetry(originUrl, retries + 1); }, 1200);
            return;
        }
        var before = originUrl || window.location.href;
        var clicked = clickBtn();
        if (!clicked) {
            if (retries < 10) {
                setTimeout(function() { clickBtnWithRetry(before, retries + 1); }, 2000);
            }
            return;
        }

        setTimeout(function() {
            if (handleSecurityCodePrompt()) {
                return;
            }
            var stillOnSamePage = window.location.href === before;
            if (stillOnSamePage && retries < 10) {
                log('等待：提交后仍停留在当前页面，2 秒后重试');
                clickBtnWithRetry(before, retries + 1);
            }
        }, 2000);
    }

    function currentHost() {
        return (window.location && window.location.host) || '';
    }

    function currentPath() {
        return (window.location && window.location.pathname) || '';
    }

    // ========== 主逻辑 ==========
    log('当前页面：Host=' + currentHost() + ' Path=' + currentPath());
    startSecurityCodeWatcher();
    startPayPalAddressWatcher();

    // OpenAI/Stripe页面
    if (currentHost().includes('pay.openai.com') || currentHost().includes('checkout.stripe.com')) {
        log('当前流程：OpenAI / Stripe 页面');
        setTimeout(function() {
            ensureOpenAIPayPalAddressForm(function(ready) {
                if (!ready) {
                    return;
                }
                getProfile(function(profile) {
                    var addr = profile.address;
                    log('当前：准备填写账单地址 ' + JSON.stringify(addr));
                    ensureCountryJP('billingCountry');
                    fillGoogleAddress('#billingAddressLine1', '#billingLocality', '#billingPostalCode', 'billingAdministrativeArea', addr, function() {
                        waitForOpenAIAddressFilled(function(filled) {
                            if (!filled) {
                                return;
                            }
                            var cb = document.getElementById('termsOfServiceConsentCheckbox');
                            if (cb && !cb.checked) { cb.click(); log('当前：已勾选同意条款'); }
                            setTimeout(function() { clickBtnWithRetry(); }, 1000);
                        });
                    });
                });
            });
        }, 2000);
        return;
    }

    // PayPal登录页 /pay
    if (currentHost().includes('paypal.com') && /^\/pay\/?$/.test(currentPath())) {
        log('当前流程：PayPal /pay 页面，准备点击创建新账号');
        setTimeout(function() {
            handlePayEntryFlow();
        }, 2000);
        return;
    }

    // PayPal验证码页 /webapps/hermes
    if (currentHost().includes('paypal.com') && window.location.href.includes('/webapps/hermes')) {
        if (window.location.href.includes('/billingweb/review')) {
            log('当前流程：PayPal review 页面，准备点同意按钮');
            releaseCheckoutSmsLease('review-page');
            setTimeout(function() {
                clickConsentButton();
            }, 1000);
            return;
        }

        log('当前流程：PayPal 短信验证码页面，准备拉码');
        setTimeout(function() {
            handleSecurityCodePrompt();
        }, 1500);
        return;
    }

    // PayPal结账页 /checkoutweb
    if (currentHost().includes('paypal.com') && currentPath().includes('/checkoutweb/signup')) {
        log('当前流程：PayPal signup 页面，准备填写地址和卡信息');
        setTimeout(function() {
            var countryChanged = ensureAnyCountryJP(['billingCountry', 'country']);
            if (countryChanged) {
                log('当前：signup 国家已切到 JP，等待表单刷新');
            }
            getProfile(function(profile) {
                var addr = profile.address;
                var card = profile.card;
                fill('email', randEmail());
                fill('cardNumber', card.number);
                fill('cardExpiry', card.expiry);
                fill('cardCvv', card.cvv);
                fill('password', randPass());
                fillJapaneseNames();
                fillJapaneseBirthdate();
                fillGoogleAddress('#billingLine1', '#billingCity', '#billingPostalCode', 'billingState', addr, function() {
                    waitForSignupAddressFilled(function(filled) {
                        if (!filled) {
                            return;
                        }
                        fillPhoneWithLease(function(ok) {
                            if (!ok) {
                                return;
                            }
                            setTimeout(function() { clickBtnWithRetry(); }, 500);
                        });
                    });
                });
            });
        }, 2000);
        return;
    }

    if (currentHost().includes('paypal.com') && currentPath().includes('/checkoutweb/')) {
        log('当前流程：PayPal checkout 页面，准备填写表单');
        setTimeout(function() {
            var country = document.getElementById('country');
            if (country && String(country.value || '').toUpperCase() !== 'JP') {
                ensureCountryJP('country');
                log('当前：国家已切到 JP，等待表单刷新');
                setTimeout(doFill, 3000);
            } else {
                doFill();
            }
        }, 2000);

        function doFill() {
            getProfile(function(profile) {
                var addr = profile.address;
                var card = profile.card;
                var email = randEmail();
                var password = randPass();
                log('当前：已生成邮箱和密码，准备填写');
                fill('email', email);
                fill('cardNumber', card.number);
                fill('cardExpiry', card.expiry);
                fill('cardCvv', card.cvv);
                fill('password', password);
                fillJapaneseNames();
                fillJapaneseBirthdate();
                fillGoogleAddress('#billingLine1', '#billingCity', '#billingPostalCode', 'billingState', addr, function() {
                    fillPhoneWithLease(function(ok) {
                        if (!ok) {
                            return;
                        }
                        setTimeout(function() { clickBtnWithRetry(); }, 500);
                    });
                });
            });
        }
        return;
    }

    log('当前页面：未匹配到处理分支');
})();
