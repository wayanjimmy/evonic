/**
 * badges.js — provenance badges for the thinking timeline.
 *
 * One place decides how a safety verdict and an approval decision are worded,
 * so the timeline never shows an automated decision as if a human made it.
 *
 * Every badge answers two questions:
 *   - who decided?   DMSS / deterministic rules / you / another agent / timeout
 *   - what was decided?  allow (executed) / review (asked) / block / declined
 *
 * The tooltip carries the evaluation detail: engine mode, model decision,
 * confidence, telemetry event id, flagged categories and reasons.
 */

import { uiIcon } from './icons.js';

const TONE_CLASS = {
    green: 'text-green-700 border-green-300 bg-green-50 dark:text-green-300 dark:border-green-800 dark:bg-green-950/40',
    red: 'text-red-700 border-red-300 bg-red-50 dark:text-red-300 dark:border-red-800 dark:bg-red-950/40',
    amber: 'text-orange-700 border-orange-300 bg-orange-50 dark:text-orange-300 dark:border-orange-800 dark:bg-orange-950/40',
    neutral: 'text-gray-600 border-gray-300 bg-gray-50 dark:text-gray-300 dark:border-gray-700 dark:bg-gray-800/60',
};

const TONE_ICON = { green: 'check', red: 'x', amber: 'alert', neutral: 'lock' };

const SOURCE_LABEL = { dmss: 'DMSS', rules: 'Rules', guard: 'Guard' };

function _decidedByLabel(approval) {
    const by = String(approval.decided_by || '');
    if (by.indexOf('agent:') === 0) return 'agent ' + by.slice(6);
    if (by === 'timeout') return 'Timeout';
    if (by === 'system:stopped') return 'Stopped';
    if (by === 'policy:api') return 'API policy';
    if (by.indexOf('user:') === 0) return 'You';
    return 'You';
}

/** Human-readable one-liner for an approval resolution. */
export function approvalLabel(approval) {
    if (!approval || !approval.decision) return null;
    const by = String(approval.decided_by || '');
    const who = _decidedByLabel(approval);
    if (approval.decision === 'approve') {
        if (by === 'timeout') return 'Timed out';
        return who === 'You' ? 'You approved' : 'Approved by ' + who;
    }
    if (by === 'timeout') return 'Timed out — rejected';
    if (by === 'policy:api') return 'Rejected (API policy)';
    if (by === 'system:stopped') return 'Stopped — rejected';
    return who === 'You' ? 'You declined' : 'Declined by ' + who;
}

function _safetyLabel(safety) {
    const source = SOURCE_LABEL[safety.source] || 'Rules';
    const decision = safety.decision || safety.level || 'allow';
    const word = decision === 'block' ? 'block'
        : decision === 'review' ? 'review'
        : 'allow';
    return source + ' ' + word;
}

function _toneFor(decision, source) {
    if (decision === 'block') return 'red';
    if (decision === 'review') return 'amber';
    // Only a model-backed verdict earns a positive tone; a plain rule pass is neutral.
    return source === 'dmss' ? 'green' : 'neutral';
}

function _safetyTooltip(safety) {
    const parts = [];
    parts.push((SOURCE_LABEL[safety.source] || 'Rules') + ' verdict');
    if (safety.mode) parts.push('mode ' + safety.mode);
    if (safety.model_decision) parts.push('model ' + safety.model_decision);
    if (safety.confidence != null) parts.push('confidence ' + safety.confidence);
    if (safety.event_id) parts.push('event ' + String(safety.event_id).slice(0, 12));
    if (safety.blocked_patterns && safety.blocked_patterns.length) {
        parts.push('flagged: ' + safety.blocked_patterns.join(', '));
    }
    if (safety.reasons && safety.reasons.length) {
        parts.push(safety.reasons[0]);
    }
    return parts.join(' · ');
}

/**
 * Describe the badge for a tool execution.
 * @param {object|null} safety   provenance from the server (`safety` payload)
 * @param {object|null} approval {approval_id, decision, decided_by}
 * @returns {{label:string, tone:string, title:string, icon:string}|null}
 */
export function describeBadge(safety, approval) {
    if (approval && approval.decision) {
        const label = approvalLabel(approval);
        const tone = approval.decision === 'approve' ? 'green' : 'red';
        const title = 'Decision: ' + label +
            (approval.decided_by ? ' (' + approval.decided_by + ')' : '') +
            (safety ? ' · ' + _safetyTooltip(safety) : '');
        return { label: label, tone: tone, title: title, icon: TONE_ICON[tone] };
    }
    if (!safety || !safety.source) return null;
    const decision = safety.decision || 'allow';
    // A plain rule pass with no model involvement is the boring default: no
    // badge, so the timeline stays quiet unless something actually decided.
    const hasModelContext = !!(safety.mode && safety.mode !== null) ||
        (safety.blocked_patterns && safety.blocked_patterns.length > 0);
    if (decision === 'allow' && safety.source !== 'dmss' && !hasModelContext) return null;
    const tone = _toneFor(decision, safety.source);
    return {
        label: _safetyLabel(safety),
        tone: tone,
        title: _safetyTooltip(safety),
        icon: tone === 'neutral' && decision === 'allow' ? 'check' : TONE_ICON[tone],
    };
}

/** Build the badge element, or null when there is nothing to show. */
export function buildBadge(safety, approval) {
    const desc = describeBadge(safety, approval);
    if (!desc) return null;
    const toneClass = TONE_CLASS[desc.tone] || TONE_CLASS.neutral;
    const $label = $('<span>').text(desc.label);
    return $('<span class="safety-badge inline-flex items-center gap-1 ml-auto pl-2 text-[10px] font-semibold px-1.5 py-0.5 rounded border whitespace-nowrap">')
        .addClass(toneClass)
        .attr('title', desc.title)
        .attr('data-safety-tone', desc.tone)
        .append(uiIcon(desc.icon, 11), $label);
}

/**
 * Render/refresh the badge on a timeline entry.  Idempotent: an entry keeps a
 * single badge keyed by its label, so replay + live events never stack.
 */
export function applyBadge($entry, safety, approval) {
    if (!$entry || !$entry.length) return;
    const $badge = buildBadge(safety, approval);
    if (!$badge) return;
    const key = $badge.attr('title') + '|' + $badge.text();
    if ($entry.find('.safety-badge').attr('data-safety-key') === key) return;
    $entry.find('.safety-badge').remove();
    $badge.attr('data-safety-key', key);
    const $status = $entry.find('.tl-status').first().parent();
    const $header = ($status && $status.length) ? $status : $entry.children().first();
    if ($header && $header.length) $header.append($badge);
    else $entry.append($badge);
}
