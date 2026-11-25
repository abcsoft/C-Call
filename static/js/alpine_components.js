/**
 * Shared Alpine.js component factories for C-Call IVR.
 * Loaded once in <head> before Alpine so hx-boost body swaps never race
 * against MutationObserver when evaluating x-data attributes.
 */
(function (global) {
    'use strict';

    global.liveCallMonitor = function (opts) {
        opts = opts || {};
        var isAdmin = opts.mode === 'admin';
        var pollUrl = opts.pollUrl || (isAdmin ? '/admin/api/live-calls' : '/api/live-calls');
        var hangupPrefix = opts.hangupPrefix || (isAdmin ? '/admin/api/live-calls' : '/api/live-calls');
        var confirmMsg = opts.confirmMsg || (isAdmin ? 'Hang up this call?' : 'Cancel this call?');
        var defaultSummary = isAdmin
            ? { active_calls: 0, ringing: 0, answered: 0, calls_today: 0, failed_today: 0 }
            : { active_calls: 0, confirmed_today: 0, cancelled_today: 0, no_answer_today: 0 };

        return {
            calls: [],
            summary: Object.assign({}, defaultSummary),
            timer: null,

            start: function () {
                this.poll();
            },
            poll: async function () {
                try {
                    var resp = await fetch(pollUrl, { headers: { 'Accept': 'application/json' } });
                    var data = await resp.json();
                    if (data && data.success) {
                        this.calls = data.calls || [];
                        this.summary = data.summary || Object.assign({}, defaultSummary);
                    }
                } catch (e) { /* keep last known state on transient error */ }
                clearTimeout(this.timer);
                var interval = this.calls.length > 0 ? 1000 : 5000;
                var self = this;
                this.timer = setTimeout(function () { self.poll(); }, interval);
            },
            hangup: async function (callId) {
                if (!confirm(confirmMsg)) return;
                var csrfMeta = document.querySelector('meta[name="csrf-token"]');
                var csrfToken = csrfMeta ? csrfMeta.content : '';
                await fetch(hangupPrefix + '/' + callId + '/hangup', {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrfToken }
                });
                this.poll();
            },
            statusBadge: function (status) {
                var map = {
                    ringing: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
                    answered: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
                    waiting_dtmf: 'bg-sky-500/10 text-sky-400 border-sky-500/20',
                    playing_tts: 'bg-sky-500/10 text-sky-400 border-sky-500/20',
                    failed: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
                    completed: 'bg-teal-500/10 text-teal-400 border-teal-500/20',
                };
                return map[status] || 'bg-slate-800 text-slate-400 border-slate-700';
            },
            formatTime: function (iso) {
                if (!iso) return '—';
                return new Date(iso).toLocaleTimeString();
            }
        };
    };

    global.retrySettings = function (config) {
        return {
            maxAttempts: config.maxAttempts,
            maxAttemptsCap: config.maxAttemptsCap,
            dndStart: config.dndStart,
            dndEnd: config.dndEnd,
            retryEnabled: config.retryEnabled,
            rows: [],
            previewText: '',
            clientErrors: [],

            init: function () {
                var byAttempt = {};
                (config.rules || []).forEach(function (r) { byAttempt[r.attempt_no] = r; });
                this.rows = [];
                for (var i = 1; i <= 5; i++) {
                    var existing = byAttempt[i];
                    this.rows.push({
                        attempt_no: i,
                        delay_value: existing ? existing.delay_value : (i === 1 ? 15 : i === 2 ? 30 : 60),
                        delay_unit: existing ? existing.delay_unit : 'minutes',
                        retry_on: existing ? existing.retry_on.slice() : ['no_answer', 'busy'],
                    });
                }
                this.recalcPreview();
            },
            delayToMinutes: function (row) {
                var mult = { minutes: 1, hours: 60, days: 1440 }[row.delay_unit] || 1;
                return (Number(row.delay_value) || 0) * mult;
            },
            validate: function (event) {
                this.clientErrors = [];
                if (!this.retryEnabled) return true;
                for (var i = 0; i < this.maxAttempts; i++) {
                    var row = this.rows[i];
                    if (!row.delay_value || Number(row.delay_value) <= 0) {
                        this.clientErrors.push('Retry ' + row.attempt_no + ': delay is required and must be positive.');
                    }
                    if (!row.retry_on.length) {
                        this.clientErrors.push('Retry ' + row.attempt_no + ': select at least one retry condition.');
                    }
                }
                if (this.dndStart === this.dndEnd) {
                    this.clientErrors.push('DND start and end times must be different.');
                }
                if (this.clientErrors.length) {
                    if (event) event.preventDefault();
                    window.scrollTo({ top: 0, behavior: 'smooth' });
                    return false;
                }
                return true;
            },
            applyDnd: function (minutesOfDay, dndStartMin, dndEndMin) {
                var inDnd;
                if (dndStartMin > dndEndMin) {
                    inDnd = minutesOfDay >= dndStartMin || minutesOfDay < dndEndMin;
                } else {
                    inDnd = minutesOfDay >= dndStartMin && minutesOfDay < dndEndMin;
                }
                if (!inDnd) return minutesOfDay;
                var wake = dndEndMin + 5;
                if (minutesOfDay >= dndStartMin && dndStartMin > dndEndMin) {
                    wake += 24 * 60;
                }
                return wake;
            },
            timeToMinutes: function (t) {
                var parts = t.split(':').map(Number);
                return parts[0] * 60 + parts[1];
            },
            formatMinutes: function (totalMinutes) {
                var overflow = Math.floor(totalMinutes / (24 * 60));
                var rem = totalMinutes % (24 * 60);
                var h = Math.floor(rem / 60) % 24;
                var m = rem % 60;
                var ampm = h >= 12 ? 'PM' : 'AM';
                var h12 = h % 12 === 0 ? 12 : h % 12;
                var label = h12 + ':' + String(m).padStart(2, '0') + ' ' + ampm;
                return overflow > 0 ? '+' + overflow + 'd ' + label : label;
            },
            recalcPreview: function () {
                if (!this.dndStart || !this.dndEnd) { this.previewText = ''; return; }
                var dndStartMin = this.timeToMinutes(this.dndStart);
                var dndEndMin = this.timeToMinutes(this.dndEnd);
                var cursor = this.timeToMinutes('14:00');
                var parts = ['If initial call at 2:00 PM goes unanswered:'];
                for (var i = 0; i < this.maxAttempts; i++) {
                    var mins = this.delayToMinutes(this.rows[i]) || 30;
                    cursor = cursor + mins;
                    cursor = this.applyDnd(cursor, dndStartMin, dndEndMin);
                    parts.push('Retry ' + (i + 1) + ': ' + this.formatMinutes(cursor));
                }
                this.previewText = parts.join(' | ');
            }
        };
    };
})(window);
