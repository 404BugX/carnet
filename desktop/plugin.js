/**
 * Carnet — interface half.
 *
 * Full-screen page in the sidebar: Facts, Core, Watch, Injections, Stats, Settings, About.
 * The theme accent is used for only three things: the active tab, the main
 * action of a screen, and the current value of an attribute. Everything else is
 * neutral surfaces — a page must not be a wall of green.
 */
import {
  ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA,
  host, useValue, useQuery, queryClient, atom,
  Badge, Button, Input, SearchField, SegmentedControl,
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
  Separator, Skeleton, EmptyState, ErrorState
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'carnet'
let _ctx = null

const tab$ = atom('facts')
// The page ships with the app; the Python half is installed per profile. In a
// profile without it every call answers 404, so the page must say so instead of
// showing an empty memory.
const backendMissing$ = atom(false)
const search$ = atom('')
const filter$ = atom('all')
const sort$ = atom('recent')
// "forget" destroys nothing: the fact moves to status 'removed'. Without this
// selector the UI could not show them, the query falling back on the API
// default 'active'.
const status$ = atom('active')
const openFact$ = atom('')
const tick$ = atom(0)   // incremented after every action → fresh query key
const modelMode$ = atom('off')   // 'custom' enables rephrase and watch

// Labels are keys into the locale files, resolved by t() at render time: no
// translation is written twice, the dashboard and the backend read the same
// files.
const TABS = [
  { id: 'facts', label: 'dashboard.tabs.facts' },
  { id: 'notes', label: 'dashboard.tabs.notes' },
  { id: 'core', label: 'dashboard.tabs.core' },
  { id: 'watch', label: 'dashboard.tabs.watch' },
  { id: 'injections', label: 'dashboard.tabs.injections' },
  { id: 'graphs', label: 'dashboard.tabs.graphs' },
  { id: 'stats', label: 'dashboard.tabs.stats' },
  { id: 'settings', label: 'dashboard.tabs.settings' },
  { id: 'about', label: 'dashboard.tabs.about' }
]

const FILTERS = [
  { id: 'all', label: 'dashboard.filters.all' },
  { id: '0', label: 'dashboard.filters.core' },
  { id: '1', label: 'dashboard.filters.facts' },
  { id: '2', label: 'dashboard.filters.sheets' }
]

const SORTS = [
  { id: 'recent', label: 'dashboard.sorts.recent' },
  { id: 'oldest', label: 'dashboard.sorts.oldest' },
  { id: 'importance', label: 'dashboard.sorts.importance' },
  { id: 'topic', label: 'dashboard.sorts.topic' },
  { id: 'text', label: 'dashboard.sorts.text' },
  { id: 'uses', label: 'dashboard.sorts.uses' }
]

const STATUSES = [
  { id: 'active', label: 'dashboard.status.active' },
  { id: 'replaced', label: 'dashboard.status.replaced' },
  { id: 'removed', label: 'dashboard.status.removed' }
]

const LEVEL = { 0: 'dashboard.levels.core', 1: 'dashboard.levels.fact', 2: 'dashboard.levels.sheet' }

function labelled(items) {
  return items.map((x) => ({ id: x.id, label: t(x.label) }))
}

async function probeBackend() {
  try {
    const settings = await api('/settings')
    backendMissing$.set(false)
    return settings
  } catch (_) {
    backendMissing$.set(true)
    return null
  }
}

function api(path, opts) {
  return _ctx.rest(path, { timeoutMs: 120000, ...(opts || {}) })
}

// ── Translations ────────────────────────────────────────────────────────────
// The tree comes from GET /strings, which reads the same locales/*.yaml files
// as the Python side. Nothing is duplicated here.
let STRINGS = {}

function t(key, vars) {
  let node = STRINGS
  for (const part of key.split('.')) {
    if (!node || typeof node !== 'object') { node = null; break }
    node = node[part]
  }
  if (typeof node !== 'string') { _retryStrings(); return key }
  if (!vars) return node
  return node.replace(/\{(\w+)\}/g, (m, k) => (vars[k] === undefined ? m : String(vars[k])))
}

// A key that does not resolve means the cached tree predates the backend that is
// serving it (an upgrade, a language file edited while the app stayed open). One
// deferred refetch heals it, with a floor so a genuinely missing key cannot loop.
let _stringsMissAt = 0
function _retryStrings() {
  const now = Date.now()
  if (now - _stringsMissAt < 4000) return
  _stringsMissAt = now
  setTimeout(() => {
    loadStrings(true)
      .then(() => { try { tick$.set(tick$.get() + 1) } catch (_) { /* not mounted yet */ } })
      .catch(() => {})
  }, 0)
}

let _strings = null
function loadStrings(force) {
  if (!_strings || force) {
    _strings = api('/strings')
      .then((tree) => { STRINGS = tree || {}; return STRINGS })
      .catch((e) => { _strings = null; throw e })
  }
  return _strings
}

function notifyError(error, what) {
  host.notify({
    kind: 'error',
    message: what || t('errors.action_failed'),
    detail: [error && error.status ? `HTTP ${error.status}` : '', String((error && error.message) || error)]
      .filter(Boolean).join(' · ')
  })
}

async function copyId(id) {
  try {
    await navigator.clipboard.writeText(id)
    host.notify({ kind: 'info', message: t('dashboard.notify.id_copied', { id }) })
  } catch (e) {
    notifyError(e, t('errors.copy_failed'))
  }
}

function refresh() {
  tick$.set(tick$.get() + 1)
  return queryClient.invalidateQueries({ queryKey: ['carnet'] })
}

function day(iso) {
  return iso ? String(iso).slice(0, 10) : ''
}

function Chip({ children, active }) {
  return jsx('span', {
    className: active
      ? 'rounded-full border border-(--ui-accent) bg-(--ui-accent)/10 px-2 py-0.5 text-[0.6875rem] text-(--ui-text-primary)'
      : 'rounded-full border border-(--border) px-2 py-0.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    children
  })
}

function Row({ label, value }) {
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-4 border-b border-(--border)/50 py-1.5',
    children: [
      jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: label }),
      jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-secondary)', children: String(value ?? '—') })
    ]
  })
}

// ── Facts ───────────────────────────────────────────────────────────────────
// Three levels, from most useful to most technical:
//   1. the header — the text, its level, its confidence, its age;
//   2. expanded: the evidence if any, the metadata on ONE line, the
//      short fields below;
//   3. "More" — the technical columns (validity, fingerprint, size, calls).
// Every editable value has its pencil: nothing is edited by accident.
const FIELD = {
  importance: { label: 'dashboard.fields.importance', type: 'select', number: true, options: [[0, 'dashboard.options.core'], [1, 'dashboard.options.fact'], [2, 'dashboard.options.sheet']] },
  confidence: { label: 'dashboard.fields.confidence', type: 'select', options: [['certain', 'dashboard.options.certain'], ['probable', 'dashboard.options.probable']] },
  status: { label: 'dashboard.fields.status', type: 'select', options: [['active', 'dashboard.options.active'], ['replaced', 'dashboard.options.replaced'], ['removed', 'dashboard.options.removed']] },
  evidence_missing: { label: 'dashboard.fields.evidence', type: 'select', number: true, options: [[0, 'dashboard.options.provided'], [1, 'dashboard.options.missing']] },
  source: { label: 'dashboard.fields.source', type: 'text' },
  created_at: { label: 'dashboard.fields.created', type: 'text', date: true },
  topic: { label: 'dashboard.fields.topic', type: 'text' },
  keywords: { label: 'dashboard.fields.keywords', type: 'text' },
  evidence: { label: 'dashboard.fields.evidence', type: 'long' },
  text: { label: 'dashboard.fields.text', type: 'long' },
  predicate: { label: 'dashboard.fields.predicate', type: 'text' },
  value: { label: 'dashboard.fields.value', type: 'text' },
  session_id: { label: 'dashboard.fields.session', type: 'text' },
  valid_from: { label: 'dashboard.fields.valid_from', type: 'text' },
  valid_until: { label: 'dashboard.fields.valid_until', type: 'text' },
  replaced_by: { label: 'dashboard.fields.replaced_by', type: 'text' }
}

// On a single line: what reads at a glance without expanding anything.
// Each slot carries its label — the value alone was unreadable.
// (These fields are NOT in "More": removing them would lose their editing.)
const META = [
  { id: 'importance', label: 'dashboard.fields.level' },
  { id: 'confidence', label: 'dashboard.fields.confidence' },
  { id: 'status', label: 'dashboard.fields.status' },
  { id: 'evidence_missing', label: 'dashboard.fields.evidence' },
  { id: 'source', label: 'dashboard.fields.source' },
  { id: 'created_at', label: 'dashboard.fields.created_on' }
]
const TAIL_FIELDS = ['topic', 'keywords']
const EXTRA_FIELDS = [
  { id: 'evidence' }, { id: 'predicate' }, { id: 'value' }, { id: 'session_id' },
  { id: 'valid_from' }, { id: 'valid_until' }, { id: 'replaced_by' },
  { id: 'updated_at', label: 'dashboard.fields.modified', readOnly: true },
  { id: 'text_hash', label: 'dashboard.fields.fingerprint', readOnly: true, mono: true },
  { id: 'size', label: 'dashboard.fields.size', readOnly: true },
  { id: 'uses', label: 'dashboard.fields.uses', readOnly: true },
  { id: 'last_used_at', label: 'dashboard.fields.last_recall', readOnly: true }
]

function _isEmpty(v) {
  return v === null || v === undefined || v === ''
}

function _display(field, v) {
  if (_isEmpty(v)) return '—'
  // A raw ISO timestamp (`2026-10-24T07:12:33+00:00`) teaches the eye nothing.
  if (field && field.date) {
    const m = String(v).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/)
    if (m) return `${m[3]}/${m[2]}/${m[1]}`
  }
  if (Array.isArray(field && field.options)) {
    const found = field.options.find(([val]) => String(val) === String(v))
    if (found) return t(found[1])
  }
  return String(v)
}

// Editing ONE value: the pencil opens the field, Enter validates, Escape cancels.
function _useField(raw, field, onSave) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const base = _isEmpty(raw) ? '' : String(raw)

  const open = (e) => {
    if (e) e.stopPropagation()
    setValue(base)
    setEditing(true)
  }
  const close = () => { setValue(base); setEditing(false) }
  const commit = async (forced) => {
    if (!editing) return
    const rawValue = forced === undefined ? value : forced
    const v = field && field.number ? Number(rawValue) : rawValue
    setEditing(false)
    if (String(v) === base) return
    await onSave({ [field.id]: v })
  }
  const keys = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); return }
    if (e.key === 'Enter' && !e.shiftKey && field.type !== 'long') { e.preventDefault(); void commit() }
  }
  return { editing, value, setValue, open, close, commit, keys }
}

function _input(field, state, extra, commitOnChoice, onBlur) {
  const props = {
    className: 'carnet-input' + (extra || ''),
    value: state.value,
    autoFocus: true,
    onChange: (e) => {
      state.setValue(e.target.value)
      if (commitOnChoice) void state.commit(e.target.value)
    },
    onKeyDown: state.keys,
    onBlur: onBlur
  }
  if (field.type === 'long') return jsx('textarea', props)
  if (field.type === 'select') {
    return jsx('select', Object.assign({}, props, {
      children: field.options.map(([v, l]) => jsx('option', { value: String(v), children: t(l) }, String(v)))
    }))
  }
  return jsx('input', Object.assign({ spellCheck: false }, props))
}

// "label — value — pencil" row, for fields that deserve their own line.
function FieldRow({ f, id, field, onSave }) {
  const c = Object.assign({ id }, field || FIELD[id] || { label: `dashboard.fields.${id}`, type: 'text' })
  const raw = f[id]
  const state = _useField(raw, c, onSave)

  return jsxs('div', {
    className: 'carnet-field',
    children: [
      jsx('span', { className: 'carnet-field-key', children: t(c.label) }),
      state.editing
        ? jsxs('div', {
            className: 'carnet-field-edit',
            children: [
              jsx('div', {
                className: 'carnet-field-input',
                children: _input(c, state, '', c.type === 'select' ? () => {} : null)
              }),
              jsxs('div', {
                className: 'carnet-field-actions',
                children: [
                  jsx('button', {
                    type: 'button', title: t('dashboard.actions.cancel'), className: 'carnet-mini carnet-mini-cancel',
                    onMouseDown: (e) => e.preventDefault(),
                    onClick: (e) => { e.stopPropagation(); state.close() },
                    children: jsx('i', { className: 'codicon codicon-close', 'aria-hidden': 'true' })
                  }),
                  jsx('button', {
                    type: 'button', title: t('dashboard.actions.save'), className: 'carnet-mini carnet-mini-confirm',
                    onMouseDown: (e) => e.preventDefault(),
                    onClick: (e) => { e.stopPropagation(); void state.commit() },
                    children: jsx('i', { className: 'codicon codicon-check', 'aria-hidden': 'true' })
                  })
                ]
              })
            ]
          })
        : jsxs('div', {
            className: 'carnet-field-value',
            children: [
              jsx('span', {
                className: _isEmpty(raw) ? 'carnet-field-val empty' : 'carnet-field-val',
                children: _display(c, raw)
              }),
              jsx('button', {
                type: 'button', title: t('dashboard.fields.edit', { label: t(c.label) }),
                className: 'carnet-field-pencil', onClick: state.open,
                children: jsx('i', { className: 'codicon codicon-edit', 'aria-hidden': 'true' })
              })
            ]
          })
    ]
  })
}

// Metadata strip value. A value alone ("missing", "agent") means nothing:
// each slot carries its label, set back.
function MetaField({ f, id, label, onSave }) {
  const c = Object.assign({ id }, FIELD[id])
  const raw = f[id]
  const state = _useField(raw, c, onSave)

  const value = state.editing
    ? jsx('span', {
        className: 'carnet-meta-edit',
        onClick: (e) => e.stopPropagation(),
        children: _input(
          c, state, ' carnet-input-meta',
          c.type === 'select' ? (v) => void state.commit(v) : null,
          () => void state.commit()
        )
      })
    : jsx('button', {
        type: 'button',
        title: t('dashboard.meta.edit', { label: t(c.label) }),
        className: _isEmpty(raw) ? 'carnet-meta-item empty' : 'carnet-meta-item',
        onClick: state.open,
        children: _display(c, raw)
      })

  if (!label) return value
  return jsxs('span', {
    className: 'carnet-meta-group',
    children: [
      jsx('span', { className: 'carnet-meta-key', children: t(label) }),
      value
    ]
  })
}

function FactRow({ f }) {
  const open = useValue(openFact$) === f.id
  const [more, setMore] = useState(false)
  const [draft, setDraft] = useState(null)   // null = reading, otherwise a draft
  const [rewriting, setRewriting] = useState('idle')   // 'idle' | 'running'
  const [proposal, setProposal] = useState(null)
  const modelMode = useValue(modelMode$)

  const toggle = () => {
    if (draft !== null) return
    if (open) { openFact$.set('') }
    else { openFact$.set(f.id); setMore(false) }
  }

  const act = async (fn, what) => {
    try { await fn(); await refresh(); openFact$.set('') } catch (e) { notifyError(e, what) }
  }

  // Editing a field does not close the card: several values are often corrected
  // in a row, and closing would lose the thread.
  const saveField = async (body) => {
    try {
      await api(`/facts/${f.id}`, { method: 'PATCH', body })
      await refresh()
    } catch (e) { notifyError(e, t('errors.saving_failed')) }
  }

  // Rephrasing: the small local model proposes a clean rewrite, and it is the
  // edit field that opens with the proposal — nothing is written until ✓ is
  // clicked.
  const rephrase = async () => {
    if (rewriting === 'running') return
    setRewriting('running')
    try {
      const r = await api(`/facts/${f.id}/rephrase`, { method: 'POST', timeoutMs: 200000 })
      if (r && r.text) setProposal(r)
    } catch (e) {
      notifyError(e, t('errors.rephrasing_failed'))
    } finally {
      setRewriting('idle')
    }
  }

  // Accepting is the only write path: until it is clicked, the fact keeps its
  // original text — a failed rephrase cannot overwrite anything.
  const accept = async () => {
    if (!proposal || proposal.over) return
    const proposedText = proposal.text
    setProposal(null)
    await saveField({ text: proposedText })
  }

  const saveText = async () => {
    const text = (draft || '').trim()
    if (!text) return
    setDraft(null)
    if (text === f.text) return
    await saveField({ text })
  }

  // "Propose to the core" forces nothing: the core has 20 seats, and the answer
  // says which of the two cases we are in. A button that fails silently would be
  // worse than no button.
  const promote = async () => {
    try {
      const res = await api(`/facts/${f.id}/promote`, { method: 'POST' })
      await refresh()
      host.notify({
        kind: res && res.seat ? 'info' : 'warning',
        message: res && res.seat ? t('dashboard.facts.core_granted') : t('dashboard.facts.no_seat'),
        detail: res && res.limit ? t('dashboard.facts.core_full', { limit: res.limit }) : undefined
      })
    } catch (e) { notifyError(e, t('errors.promotion_failed')) }
  }

  return jsxs('div', {
    className: 'border-b border-(--border)/60 last:border-0',
    children: [
      jsxs('div', {
        className: 'flex cursor-pointer items-start gap-3 px-2.5 py-2 hover:bg-(--chrome-action-hover)/40',
        onClick: toggle,
        children: [
          jsx('span', {
            className: 'mt-0.5 w-11 shrink-0 text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)',
            children: LEVEL[f.importance] ? t(LEVEL[f.importance]) : f.importance
          }),
          jsxs('div', {
            className: 'flex min-w-0 flex-1 flex-col gap-0.5',
            children: [
              draft === null
                ? jsxs('span', {
                    className: 'carnet-head-text',
                    children: [
                      jsx('span', {
                        className: open || draft !== null ? 'carnet-head-val' : 'carnet-head-val clamp',
                        children: f.text
                      }),
                      jsx('button', {
                        type: 'button', title: t('dashboard.facts.edit_text'),
                        className: 'carnet-field-pencil carnet-head-pencil',
                        onClick: (e) => { e.stopPropagation(); setDraft(f.text) },
                        children: jsx('i', { className: 'codicon codicon-edit', 'aria-hidden': 'true' })
                      })
                    ]
                  })
                : jsxs('div', {
                    className: 'carnet-head-edit',
                    onClick: (e) => e.stopPropagation(),
                    children: [
                      jsx('textarea', {
                        className: 'carnet-input',
                        value: draft,
                        autoFocus: true,
                        onChange: (e) => setDraft(e.target.value),
                        onKeyDown: (e) => {
                          if (e.key === 'Escape') { e.stopPropagation(); setDraft(null) }
                          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void saveText() }
                        }
                      }),
                      jsxs('div', {
                        className: 'carnet-field-actions',
                        children: [
                          jsx('button', {
                            type: 'button', title: t('dashboard.actions.cancel'), className: 'carnet-mini carnet-mini-cancel',
                            onMouseDown: (e) => e.preventDefault(),
                            onClick: () => setDraft(null),
                            children: jsx('i', { className: 'codicon codicon-close', 'aria-hidden': 'true' })
                          }),
                          jsx('button', {
                            type: 'button', title: t('dashboard.actions.save'), className: 'carnet-mini carnet-mini-confirm',
                            onMouseDown: (e) => e.preventDefault(),
                            onClick: () => void saveText(),
                            children: jsx('i', { className: 'codicon codicon-check', 'aria-hidden': 'true' })
                          })
                        ]
                      })
                    ]
                  }),
              jsxs('span', {
                className: 'flex flex-wrap items-center gap-2 text-[0.625rem] text-(--ui-text-quaternary)',
                children: [
                  jsx('span', {
                    title: (f.uses || 0) > 0
                      ? t('dashboard.facts.recalled', { count: f.uses, date: day(f.last_used_at) || t('dashboard.facts.unknown') })
                      : t('dashboard.facts.never_recalled'),
                    className: (f.uses || 0) > 0
                      ? 'inline-flex items-center gap-0.5 rounded-full border border-(--border) bg-(--ui-bg-quaternary) px-1.5 py-[1px] font-mono text-[0.6875rem] text-(--ui-text-secondary)'
                      : 'inline-flex items-center gap-0.5 rounded-full border border-(--border)/45 px-1.5 py-[1px] font-mono text-[0.6875rem] text-(--ui-text-quaternary)',
                    children: `↻ ${f.uses || 0}`
                  }),
                  f.topic ? jsx('span', { children: f.topic }) : null,
                  jsx('span', { children: f.confidence }),
                  jsx('span', { children: day(f.created_at) }),
                  jsx('button', {
                    type: 'button',
                    title: t('dashboard.facts.copy_id', { id: f.id }),
                    className: 'rounded border border-(--border)/60 px-1.5 py-[1px] font-mono text-[0.625rem] text-(--ui-text-quaternary) hover:border-(--border) hover:text-(--ui-text-secondary)',
                    onClick: (e) => {
                      e.stopPropagation()
                      copyId(f.id)
                    },
                    children: `${f.id} ⧉`
                  })
                ]
              })
            ]
          })
        ]
      }),
      open
        ? jsxs('div', {
            className: 'carnet-detail',
            children: [
              _isEmpty(f.evidence) ? null : jsx(FieldRow, { f, id: 'evidence', onSave: saveField }),
              jsx('div', {
                className: 'carnet-meta',
                children: META.map((m, i) => jsxs('span', {
                  className: 'carnet-meta-slot',
                  children: [
                    i > 0 ? jsx('span', { className: 'carnet-meta-sep', children: '·' }) : null,
                    jsx(MetaField, { f, id: m.id, label: m.label, onSave: saveField }, m.id)
                  ]
                }, m.id))
              }),
              TAIL_FIELDS.map((id) => jsx(FieldRow, { f, id, onSave: saveField }, id)),
              more
                ? EXTRA_FIELDS.map((c) => (c.readOnly
                    ? jsxs('div', {
                        className: 'carnet-field',
                        children: [
                          jsx('span', { className: 'carnet-field-key', children: t(c.label) }),
                          jsx('span', {
                            className: `carnet-field-val${c.mono ? ' mono' : ''}${_isEmpty(f[c.id]) ? ' empty' : ''}`,
                            children: _display(c, f[c.id])
                          })
                        ]
                      }, c.id)
                    : jsx(FieldRow, { f, id: c.id, onSave: saveField }, c.id)))
                : null,
              proposal
                ? jsxs('div', {
                    className: 'carnet-proposal',
                    onClick: (e) => e.stopPropagation(),
                    children: [
                      jsxs('div', {
                        className: 'carnet-proposal-head',
                        children: [
                          jsx('span', { className: 'carnet-proposal-title', children: t('dashboard.facts.proposal_title') }),
                          jsx('span', {
                            className: proposal.over ? 'carnet-proposal-length over' : 'carnet-proposal-length',
                            children: proposal.limit
                              ? t('dashboard.facts.proposal_length', { length: proposal.length, limit: proposal.limit })
                              : t('dashboard.facts.proposal_chars', { length: proposal.length })
                          })
                        ]
                      }),
                      jsx('div', { className: 'carnet-proposal-text', children: proposal.text }),
                      jsxs('div', {
                        className: 'carnet-proposal-foot',
                        children: [
                          proposal.over
                            ? jsx('span', { className: 'carnet-proposal-note', children: t('dashboard.facts.too_long') })
                            : null,
                          jsx('div', { className: 'flex-1' }),
                          jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setProposal(null), children: t('dashboard.actions.reject') }),
                          jsx(Button, {
                            size: 'sm', variant: 'outline',                            disabled: proposal.over,
                            onClick: () => void accept(),
                            children: t('dashboard.actions.accept')
                          })
                        ]
                      })
                    ]
                  })
                : null,
              jsxs('div', {
                className: 'carnet-detail-foot',
                children: [
                  jsxs('button', {
                    type: 'button',
                    className: 'carnet-discreet',
                    onClick: (e) => { e.stopPropagation(); setMore(!more) },
                    children: [
                      jsx('i', { className: `codicon codicon-chevron-${more ? 'up' : 'down'}`, 'aria-hidden': 'true' }),
                      jsx('span', { children: more ? t('dashboard.actions.less') : t('dashboard.actions.more') })
                    ]
                  }),
                  jsx('div', { className: 'flex-1' }),
                  // One verb per button, no accent colour: the bar must not
                  // shout louder than the memory it describes.
                  modelMode === 'custom'
                    ? jsx(Button, {
                        size: 'sm', variant: 'outline',
                        disabled: rewriting === 'running',
                        onClick: () => void rephrase(),
                        children: rewriting === 'running'
                          ? jsx('i', { className: 'codicon codicon-loading codicon-modifier-spin', 'aria-hidden': 'true' })
                          : t('dashboard.actions.rephrase')
                      })
                    : null,
                  jsx(Button, {
                    size: 'sm', variant: 'outline',
                    onClick: () => copyId(f.id),
                    children: t('dashboard.actions.copy')
                  }),
                  jsx(Button, {
                    size: 'sm', variant: 'outline',
                    onClick: () => void promote(),
                    children: t('dashboard.actions.promote')
                  }),
                  jsx(Button, {
                    size: 'sm', variant: 'ghost',
                    onClick: () => act(() => api(`/facts/${f.id}/remove?reason=dashboard`, { method: 'POST' }), t('errors.removal_failed')),
                    children: t('dashboard.actions.forget')
                  })
                ]
              })
            ]
          })
        : null
    ]
  })
}

function FactsView() {
  const filters = labelled(FILTERS)
  const sortOptions = labelled(SORTS)
  const statusOptions = labelled(STATUSES)
  const q = useValue(search$)
  const filter = useValue(filter$)
  const sort = useValue(sort$)
  const status = useValue(status$)
  const tick = useValue(tick$)

  // The query only fires on a stabilized value: typing a letter must neither
  // rerun the backend on every keystroke nor replace the tree with a skeleton —
  // in both cases the search field unmounts and focus is lost after one or two
  // characters.
  const [qQuery, setQQuery] = useState(q)
  useEffect(() => {
    if (q === qQuery) return undefined
    const timer = setTimeout(() => setQQuery(q), 220)
    return () => clearTimeout(timer)
  }, [q, qQuery])

  const { data, error, isFetching } = useQuery({
    queryKey: ['carnet', 'facts', qQuery, filter, sort, status, tick],
    queryFn: () => api(`/facts?q=${encodeURIComponent(qQuery)}&sort=${sort}&status=${status}&limit=300`
      + (filter === 'all' ? '' : `&importance=${filter}`)),
    staleTime: 10000,
    // Previous results stay displayed during the refetch: the view never goes
    // back through the "no data" state.
    placeholderData: (previous) => previous
  })

  if (error && !data) return jsx(ErrorState, { title: t('errors.backend_unreachable'), message: String(error.message || error) })
  const facts = (data && data.facts) || []

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('div', {
            className: 'min-w-52 flex-1',
            children: jsx(SearchField, {
              placeholder: t('dashboard.facts.placeholder'), value: q, onChange: (v) => search$.set(v)
            })
          }),
          jsx(SegmentedControl, { value: filter, onChange: (v) => filter$.set(v), options: filters }),
          jsxs(Select, {
            value: sort, onValueChange: (v) => sort$.set(v),
            children: [
              jsx(SelectTrigger, { className: 'h-8 w-40 text-xs', children: jsx(SelectValue, {}) }),
              jsx(SelectContent, { children: sortOptions.map((s) => jsx(SelectItem, { value: s.id, children: s.label }, s.id)) })
            ]
          }),
          jsxs(Select, {
            value: status, onValueChange: (v) => status$.set(v),
            children: [
              jsx(SelectTrigger, { className: 'h-8 w-32 text-xs', children: jsx(SelectValue, {}) }),
              jsx(SelectContent, { children: statusOptions.map((s) => jsx(SelectItem, { value: s.id, children: s.label }, s.id)) })
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
        children: t('dashboard.facts.count', { count: (data && data.total) || 0 })
          + (isFetching && data ? ` · ${t('dashboard.facts.refreshing')}` : '')
      }),
      !data
        ? jsx(Skeleton, { className: 'h-32 w-full' })
        : facts.length === 0
          ? jsx(EmptyState, { title: t('dashboard.facts.empty_title'), message: t('dashboard.facts.empty_message') })
          : jsx('div', {
              className: 'min-h-0 flex-1 overflow-y-auto rounded-md border border-(--border)/60',
              children: facts.map((f) => jsx(FactRow, { f }, f.id))
            })
    ]
  })
}

// ── Core ────────────────────────────────────────────────────────────────────
function CoreView() {
  const tick = useValue(tick$)
  const { data, isLoading, error } = useQuery({
    queryKey: ['carnet', 'core', tick], queryFn: () => api('/core'), staleTime: 0
  })
  if (error) return jsx(ErrorState, { title: t('errors.backend_unreachable'), message: String(error.message || error) })
  if (isLoading && !data) return jsx(Skeleton, { className: 'h-32 w-full' })
  const seats = (data && data.seats) || []
  const limit = (data && data.limit) || 20

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-3',
        children: [
          jsx('div', {
            className: 'h-1 flex-1 overflow-hidden rounded-full bg-(--ui-bg-quaternary)',
            children: jsx('div', {
              className: 'h-full rounded-full bg-(--ui-accent)',
              style: { width: `${(seats.length / limit) * 100}%` }
            })
          }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: t('dashboard.core.seats', { used: seats.length, limit }) })
        ]
      }),
      jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
        children: t('dashboard.core.note')
      }),
      jsx('div', {
        className: 'min-h-0 flex-1 overflow-y-auto rounded-md border border-(--border)/60',
        children: seats.map((s, i) => jsxs('div', {
          className: 'flex items-start gap-3 border-b border-(--border)/60 px-2.5 py-2 last:border-0',
          children: [
            jsx('span', { className: 'w-5 shrink-0 text-right font-mono text-[0.625rem] text-(--ui-text-quaternary)', children: i + 1 }),
            jsxs('div', {
              className: 'flex min-w-0 flex-col gap-0.5',
              children: [
                jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: s.text }),
                jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.core.joined', { date: day(s.created_at) }) })
              ]
            })
          ]
        }, s.id))
      })
    ]
  })
}

// ── Notes ───────────────────────────────────────────────────────────────────
function NotesView() {
  const tick = useValue(tick$)
  const { data, isLoading, error } = useQuery({
    queryKey: ['carnet', 'notes', tick], queryFn: () => api('/notes'), staleTime: 0
  })
  if (error) return jsx(ErrorState, { title: t('errors.backend_unreachable'), message: String(error.message || error) })
  if (isLoading && !data) return jsx(Skeleton, { className: 'h-32 w-full' })
  const notes = (data && data.notes) || []

  const remove = async (id) => {
    try {
      await api(`/notes/${id}/delete`, { method: 'POST' })
      await refresh()
    } catch (e) { notifyError(e, t('errors.note_delete_failed')) }
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2',
    children: [
      jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
        children: t('dashboard.notes.count', { count: notes.length })
      }),
      notes.length
        ? jsx('div', {
            className: 'flex min-h-0 flex-1 flex-col overflow-y-auto rounded-md border border-(--border)/60',
            children: notes.map((n) => jsxs('div', {
              className: 'flex items-start gap-3 border-b border-(--border)/60 px-2.5 py-2 last:border-0',
              children: [
                jsx('span', {
                  className: 'w-9 shrink-0 font-mono text-[0.625rem] text-(--ui-text-quaternary)',
                  children: t('dashboard.notes.age', { days: n.age_days })
                }),
                jsxs('div', {
                  className: 'flex min-w-0 flex-1 flex-col gap-0.5',
                  children: [
                    jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: n.text }),
                    jsx('span', {
                      className: 'text-[0.625rem] text-(--ui-text-quaternary)',
                      children: t('dashboard.notes.expires', { date: day(n.expires_at) })
                    })
                  ]
                }),
                jsx(Button, {
                  size: 'sm', variant: 'ghost', onClick: () => void remove(n.id),
                  children: t('dashboard.actions.delete')
                })
              ]
            }, n.id))
          })
        : jsx(EmptyState, {
            title: t('dashboard.notes.empty_title'),
            message: t('dashboard.notes.empty_message')
          })
    ]
  })
}

// ── Watch ───────────────────────────────────────────────────────────────────
function WatchView() {
  const [running, setRunning] = useState(false)
  const modelMode = useValue(modelMode$)
  const tick = useValue(tick$)
  const { data, isLoading, error } = useQuery({
    queryKey: ['carnet', 'proposals', tick], queryFn: () => api('/proposals'), staleTime: 0
  })
  if (error) return jsx(ErrorState, { title: t('errors.backend_unreachable'), message: String(error.message || error) })
  if (isLoading && !data) return jsx(Skeleton, { className: 'h-32 w-full' })
  const props = (data && data.proposals) || []

  const act = async (pid, action, what) => {
    try {
      await api(`/proposals/${pid}/${action}`, { method: 'POST' })
      await refresh()
    } catch (e) { notifyError(e, what) }
  }

  const scan = async () => {
    setRunning(true)
    try {
      const r = await api('/watch/scan', { method: 'POST' })
      if (r && r.disabled) {
        host.notify({ kind: 'info', message: t('dashboard.watch.model_off') })
        await refresh()
        return
      }
      host.notify({
        kind: 'info', message: t('dashboard.watch.finished'),
        detail: t('dashboard.watch.examined', { proposals: r.proposals, pairs: r.candidates })
      })
      await refresh()
    } catch (e) { notifyError(e, t('errors.watch_failed')) } finally { setRunning(false) }
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-3',
        children: [
          jsx(Button, {
            size: 'sm', onClick: scan, disabled: running || modelMode !== 'custom',
            children: running ? t('dashboard.watch.running') : t('dashboard.actions.scan')
          }),
          jsx('span', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: t('dashboard.watch.pending', { count: props.length })
          }),
          modelMode !== 'custom'
            ? jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.watch.model_off') })
            : null
        ]
      }),
      props.length === 0
        ? jsx(EmptyState, { title: t('dashboard.watch.empty_title'), message: t('dashboard.watch.empty_message') })
        : jsx('div', {
            className: 'flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto',
            children: props.map((p) => jsxs('div', {
              className: 'flex flex-col gap-2 rounded-md border border-(--border)/70 p-2.5',
              children: [
                jsxs('div', {
                  className: 'flex items-center gap-2',
                  children: [
                    jsx(Badge, { variant: p.type === 'contradiction' ? 'warn' : 'muted', children: p.type }),
                    jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.watch.similarity', { score: p.score }) })
                  ]
                }),
                jsxs('div', {
                  className: 'grid grid-cols-2 gap-2',
                  children: [
                    jsxs('div', {
                      className: 'rounded border border-(--border)/60 bg-(--ui-bg-quaternary)/40 p-2',
                      children: [
                        jsxs('div', { className: 'mb-0.5 flex items-center gap-1.5', children: [
                          jsx('span', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: 'A' }),
                          jsx('button', {
                            type: 'button',
                            title: t('dashboard.watch.show_title'),
                            className: 'text-[0.625rem] text-(--ui-text-quaternary) underline decoration-dotted hover:text-(--ui-text-secondary)',
                            onClick: () => { search$.set(p.fact_a || ''); tab$.set('facts') },
                            children: t('dashboard.watch.show')
                          })
                        ] }),
                        jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: p.text_a || '' })
                      ]
                    }),
                    jsxs('div', {
                      className: 'rounded border border-(--border)/60 bg-(--ui-bg-quaternary)/40 p-2',
                      children: [
                        jsxs('div', { className: 'mb-0.5 flex items-center gap-1.5', children: [
                          jsx('span', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: 'B' }),
                          jsx('button', {
                            type: 'button',
                            title: t('dashboard.watch.show_title'),
                            className: 'text-[0.625rem] text-(--ui-text-quaternary) underline decoration-dotted hover:text-(--ui-text-secondary)',
                            onClick: () => { search$.set(p.fact_b || ''); tab$.set('facts') },
                            children: t('dashboard.watch.show')
                          })
                        ] }),
                        jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: p.text_b || '' })
                      ]
                    })
                  ]
                }),
                jsxs('div', {
                  className: 'rounded border border-(--ui-accent)/40 bg-(--ui-accent)/5 p-2',
                  children: [
                    jsx('div', { className: 'mb-0.5 text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: t('dashboard.watch.proposal_title') }),
                    jsx('div', {
                      className: 'text-xs text-(--ui-text-secondary)',
                      children: (p.text || '').trim().toLowerCase().startsWith('undetermined')
                        ? t('dashboard.watch.no_version')
                        : (p.text || 'undetermined')
                    })
                  ]
                }),
                p.reason ? jsx('div', { className: 'text-[0.625rem] italic text-(--ui-text-quaternary)', children: p.reason }) : null,
                jsxs('div', {
                  className: 'flex flex-wrap gap-1.5',
                  children: [
                    (p.type === 'contradiction' && (!p.text || p.text.trim().toLowerCase().startsWith('undetermined')))
                      ? jsxs('div', { className: 'flex flex-col gap-1', children: [
                          jsxs('div', { className: 'flex gap-1.5', children: [
                            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => act(p.id, 'accept?keep=a', t('errors.applying_failed')), children: t('dashboard.actions.keep_a') }),
                            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => act(p.id, 'accept?keep=b', t('errors.applying_failed')), children: t('dashboard.actions.keep_b') })
                          ] }),
                          jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.watch.conflict_note') })
                        ] })
                      : jsx(Button, { size: 'sm', onClick: () => act(p.id, 'accept', t('errors.applying_failed')), children: t('dashboard.actions.accept') }),
                    jsx(Button, { size: 'sm', variant: 'outline', onClick: () => act(p.id, 'regenerate', t('errors.regenerating_failed')), children: t('dashboard.actions.regenerate') }),
                    jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => act(p.id, 'reject', t('errors.rejection_failed')), children: t('dashboard.actions.reject') })
                  ]
                })
              ]
            }, p.id))
          })
    ]
  })
}


// ── Settings ────────────────────────────────────────────────────────────────
// Everything that changes how the plugin behaves: the language (applies at
// once) and the embedding backend (applies at the next session start).
function Field({ label, value, onChange, placeholder, type }) {
  return jsxs('label', {
    className: 'flex flex-col gap-1',
    children: [
      jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: label }),
      jsx(Input, { value, onChange, placeholder, type: type || 'text' })
    ]
  })
}

function fmtBytes(n) {
  const v = Number(n) || 0
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(1)} GB`
  return `${Math.round(v / (1024 ** 2))} MB`
}

// ── Status bar ──────────────────────────────────────────────────────────────
// A quiet counter: one monochrome glyph, one small number. The opacity change// ── Memory settings ─────────────────────────────────────────────────────────
function NumberSetting({ field, value, onChange, prefix = 'memory' }) {
  const key = field.key
  return jsxs('label', {
    className: 'flex flex-col gap-1',
    children: [
      jsx('span', { className: 'text-[0.625rem] text-(--ui-text-secondary)', children: t(`dashboard.settings.${prefix}_${key}`) }),
      jsx('input', {
        type: 'number', className: 'carnet-input',
        min: field.min, max: field.max, step: field.step, value: String(value),
        onChange: (e) => onChange(key, e.target.value)
      }),
      jsx('span', {
        className: 'text-[0.5625rem] leading-tight text-(--ui-text-quaternary)',
        children: t(`dashboard.settings.${prefix}_${key}_help`)
      })
    ]
  })
}

function MemorySettings() {
  const tick = useValue(tick$)
  const { data } = useQuery({
    queryKey: ['carnet', 'memory-settings', tick], queryFn: () => api('/settings/memory'), staleTime: 0
  })
  const [values, setValues] = useState(null)
  useEffect(() => {
    if (!data) return
    setValues(Object.fromEntries((data.fields || []).map((f) => [f.key, f.value])))
  }, [data])
  if (!data || !values) return jsx(Skeleton, { className: 'h-24 w-full' })

  const save = async () => {
    try {
      const res = await api('/settings/memory', { method: 'POST', body: { values } })
      setValues(Object.fromEntries((res.fields || []).map((f) => [f.key, f.value])))
      await refresh()
      host.notify({ kind: 'info', message: t('dashboard.settings.memory_saved') })
    } catch (e) { notifyError(e, t('errors.memory_failed')) }
  }
  const reset = async () => {
    try {
      const res = await api('/settings/memory/reset', { method: 'POST' })
      setValues(Object.fromEntries((res.fields || []).map((f) => [f.key, f.value])))
      await refresh()
      host.notify({ kind: 'info', message: t('dashboard.settings.memory_reset') })
    } catch (e) { notifyError(e, t('errors.memory_failed')) }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-secondary)', children: t('dashboard.settings.memory') }),
      jsx('div', {
        className: 'grid grid-cols-2 gap-x-3 gap-y-2',
        children: (data.fields || []).map((f) => jsx(NumberSetting, {
          field: f, value: values[f.key],
          onChange: (key, raw) => setValues({ ...values, [key]: raw })
        }, f.key))
      }),
      jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.settings.memory_note') }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Button, { size: 'sm', onClick: save, children: t('dashboard.settings.embedding_save') }),
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: reset, children: t('dashboard.settings.reset') })
        ]
      })
    ]
  })
}

// ── Notes settings ──────────────────────────────────────────────────────────
function NotesSettings() {
  const tick = useValue(tick$)
  const { data } = useQuery({
    queryKey: ['carnet', 'notes-settings', tick], queryFn: () => api('/settings/notes'), staleTime: 0
  })
  const [enabled, setEnabled] = useState(false)
  const [values, setValues] = useState(null)
  useEffect(() => {
    if (!data) return
    setEnabled(data.enabled === true)
    setValues(Object.fromEntries((data.fields || []).map((f) => [f.key, f.value])))
  }, [data])
  if (!data || !values) return jsx(Skeleton, { className: 'h-20 w-full' })

  const save = async () => {
    try {
      const res = await api('/settings/notes', {
        method: 'POST', body: { enabled, ...values }
      })
      setEnabled(res.enabled === true)
      setValues(Object.fromEntries((res.fields || []).map((f) => [f.key, f.value])))
      await refresh()
      host.notify({ kind: 'info', message: t('dashboard.settings.notes_saved') })
    } catch (e) { notifyError(e, t('errors.notes_failed')) }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-secondary)', children: t('dashboard.settings.notes') }),
      jsxs('label', {
        className: 'flex items-center gap-2 text-[0.625rem] text-(--ui-text-tertiary)',
        children: [
          jsx('input', { type: 'checkbox', checked: enabled, onChange: (e) => setEnabled(e.target.checked) }),
          jsx('span', { children: t('dashboard.settings.notes_enabled') })
        ]
      }),
      jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.settings.notes_note') }),
      jsx('div', {
        className: 'grid grid-cols-2 gap-x-3 gap-y-2',
        children: (data.fields || []).map((f) => jsx(NumberSetting, {
          field: f, value: values[f.key], prefix: 'notes',
          onChange: (key, raw) => setValues({ ...values, [key]: raw })
        }, f.key))
      }),
      jsx('div', {
        className: 'flex items-center justify-start',
        children: jsx(Button, { size: 'sm', onClick: save, children: t('dashboard.settings.embedding_save') })
      })
    ]
  })
}

// ── Model settings (rephrase and watch) ─────────────────────────────────────
function ModelSettings() {
  const tick = useValue(tick$)
  const { data } = useQuery({
    queryKey: ['carnet', 'model-settings', tick], queryFn: () => api('/settings/model'), staleTime: 0
  })
  const [draft, setDraft] = useState(null)
  useEffect(() => {
    if (!data) return
    setDraft({ mode: data.mode, url: data.url || '', model: data.model || '', api_key: '' })
  }, [data])
  if (!data || !draft) return jsx(Skeleton, { className: 'h-20 w-full' })

  const edit = (patch) => setDraft({ ...draft, ...patch })
  const save = async () => {
    try {
      await api('/settings/model', {
        method: 'POST',
        body: {
          mode: draft.mode, url: draft.url, model: draft.model,
          api_key: draft.api_key ? draft.api_key : null
        }
      })
      modelMode$.set(draft.mode)
      edit({ api_key: '' })
      await refresh()
      host.notify({ kind: 'info', message: t('dashboard.settings.model_saved') })
    } catch (e) { notifyError(e, t('errors.model_settings_failed')) }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-secondary)', children: t('dashboard.settings.model') }),
      jsx(SegmentedControl, {
        value: draft.mode,
        onChange: (mode) => edit({ mode }),
        options: [
          { id: 'off', label: t('dashboard.settings.model_off') },
          { id: 'lmstudio', label: t('dashboard.settings.model_lmstudio') },
          { id: 'custom', label: t('dashboard.settings.model_custom') }
        ]
      }),
      jsx('div', {
        className: 'text-[0.625rem] text-(--ui-text-quaternary)',
        children: t(draft.mode === 'custom' ? 'dashboard.settings.model_custom_note'
          : draft.mode === 'lmstudio' ? 'dashboard.settings.model_lmstudio_note'
            : 'dashboard.settings.model_off_note')
      }),
      draft.mode === 'custom' || draft.mode === 'lmstudio'
        ? jsxs('div', {
            className: 'flex flex-col gap-2',
            children: [
              jsx(Field, {
                label: t('dashboard.settings.embedding_url'), value: draft.url,
                onChange: (url) => edit({ url }), placeholder: 'http://127.0.0.1:1234'
              }),
              jsx(Field, {
                label: t('dashboard.settings.embedding_model'), value: draft.model,
                onChange: (model) => edit({ model }), placeholder: 'gemma-4-e2b-it'
              }),
              draft.mode === 'custom'
                ? jsx(Field, {
                    label: t('dashboard.settings.embedding_key'), value: draft.api_key,
                    onChange: (api_key) => edit({ api_key }), type: 'password',
                    placeholder: data.api_key_set ? t('dashboard.settings.embedding_key_keep') : ''
                  })
                : null
            ]
          })
        : null,
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Button, { size: 'sm', onClick: save, children: t('dashboard.settings.embedding_save') }),
          jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.settings.memory_note') })
        ]
      })
    ]
  })
}

function SettingsView() {
  const tick = useValue(tick$)
  const { data, isLoading, error } = useQuery({
    queryKey: ['carnet', 'settings', tick], queryFn: () => api('/settings'), staleTime: 0
  })
  const { data: emb } = useQuery({
    queryKey: ['carnet', 'embedding', tick], queryFn: () => api('/settings/embedding'), staleTime: 0
  })
  const [draft, setDraft] = useState(null)
  const [consent, setConsent] = useState(false)
  const downloading = !!(emb && emb.local && emb.local.state === 'downloading')

  useEffect(() => {
    if (!emb) return
    setDraft({ mode: emb.mode, url: emb.url || '', model: emb.model || '', api_key: '' })
  }, [emb])

  // While the model downloads, the backend state is the only truth: poll it.
  useEffect(() => {
    if (!downloading) return undefined
    const timer = setInterval(() => { void refresh() }, 1000)
    return () => clearInterval(timer)
  }, [downloading])

  if (error) return jsx(ErrorState, { title: t('errors.backend_unreachable'), message: String(error.message || error) })
  if ((isLoading && !data) || !emb || !draft) return jsx(Skeleton, { className: 'h-32 w-full' })
  const effective = data.language
  const sources = {
    config: 'dashboard.settings.source_config',
    hermes: 'dashboard.settings.source_hermes',
    default: 'dashboard.settings.source_default'
  }
  const options = (data.languages || []).map((code) => ({
    id: code, label: t(`dashboard.settings.language_${code}`)
  }))
  const choose = async (code) => {
    if (!code || code === effective) return
    try {
      await api('/settings/language', { method: 'POST', body: { language: code } })
      await loadStrings(true)
      await refresh()
    } catch (e) { notifyError(e, t('errors.settings_failed')) }
  }

  const modes = ['disabled', 'lmstudio', 'custom', 'local'].map((mode) => ({
    id: mode, label: t(`dashboard.settings.embedding_${mode}`)
  }))
  const local = emb.local || { state: 'absent', bytes: 0, expected: 0, percent: 0, path: '', downloaded: false }
  const edit = (patch) => setDraft({ ...draft, ...patch })
  const saveEmbedding = async () => {
    try {
      await api('/settings/embedding', {
        method: 'POST',
        body: {
          mode: draft.mode, url: draft.url, model: draft.model,
          api_key: draft.api_key ? draft.api_key : null
        }
      })
      edit({ api_key: '' })
      await refresh()
      host.notify({ kind: 'info', message: t('dashboard.settings.embedding_saved') })
    } catch (e) { notifyError(e, t('errors.embedding_failed')) }
  }
  const startDownload = async () => {
    try {
      await api('/settings/embedding/download', { method: 'POST', body: { consent: true } })
      setConsent(false)
      await refresh()
    } catch (e) { notifyError(e, t('errors.embedding_download_failed')) }
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-3 overflow-y-auto',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-secondary)', children: t('dashboard.settings.language') }),
      options.length
        ? jsx(SegmentedControl, { value: effective, onChange: choose, options })
        : null,
      jsxs('div', {
        className: 'flex flex-col',
        children: [
          jsx(Row, { label: t('dashboard.settings.effective'), value: t(`dashboard.settings.language_${effective}`) }),
          jsx(Row, { label: t('dashboard.settings.source'), value: t(sources[data.source] || sources.default) })
        ]
      }),

      jsx(Separator, {}),
      jsx(Separator, {}),
      jsxs('div', { className: 'grid grid-cols-1 gap-6 items-start md:grid-cols-2', children: [ jsxs('div', { className: 'flex flex-col gap-2', children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-secondary)', children: t('dashboard.settings.embedding') }),
      jsx(SegmentedControl, { value: draft.mode, onChange: (mode) => edit({ mode }), options: modes }),
      jsx('div', {
        className: 'text-[0.625rem] text-(--ui-text-quaternary)',
        children: t(`dashboard.settings.embedding_${draft.mode}_note`)
      }),
      draft.mode === 'lmstudio' || draft.mode === 'custom'
        ? jsxs('div', {
            className: 'flex flex-col gap-2',
            children: [
              jsx(Field, {
                label: t('dashboard.settings.embedding_url'), value: draft.url,
                onChange: (url) => edit({ url }), placeholder: 'http://127.0.0.1:1234'
              }),
              jsx(Field, {
                label: t('dashboard.settings.embedding_model'), value: draft.model,
                onChange: (model) => edit({ model }), placeholder: 'embeddinggemma'
              }),
              draft.mode === 'custom'
                ? jsx(Field, {
                    label: t('dashboard.settings.embedding_key'), value: draft.api_key,
                    onChange: (api_key) => edit({ api_key }), type: 'password',
                    placeholder: emb.api_key_set ? t('dashboard.settings.embedding_key_keep') : ''
                  })
                : null
            ]
          })
        : null,
      draft.mode === 'local'
        ? jsxs('div', {
            className: 'flex flex-col gap-1.5 rounded-md border border-(--border)/50 px-2.5 py-2',
            children: [
              jsx(Row, { label: t('dashboard.settings.local_status'), value: t(`dashboard.settings.local_${local.state}`) }),
              jsx(Row, {
                label: t('dashboard.settings.local_size'),
                value: `${fmtBytes(local.bytes)} / ${fmtBytes(local.expected)}`
              }),
              jsx(Row, { label: t('dashboard.settings.local_path'), value: local.path }),
              local.error
                ? jsx('div', { className: 'text-[0.625rem] text-(--ui-danger)', children: local.error })
                : null,
              !local.downloaded && local.percent > 0
                ? jsxs('div', {
                    className: 'flex flex-col gap-1',
                    children: [
                      jsx('div', {
                        className: 'h-1 overflow-hidden rounded-full bg-(--ui-text-quaternary)/15',
                        children: jsx('div', {
                          className: 'h-full rounded-full bg-(--ui-accent)',
                          style: { width: `${Math.max(2, local.percent)}%` }
                        })
                      }),
                      jsx('span', {
                        className: 'font-mono text-[0.5625rem] text-(--ui-text-quaternary)',
                        children: `${local.percent}%`
                      })
                    ]
                  })
                : null,
              !local.downloaded
                ? jsxs('label', {
                    className: 'flex items-start gap-2 pt-1 text-[0.625rem] text-(--ui-text-tertiary)',
                    children: [
                      jsx('input', {
                        type: 'checkbox', checked: consent,
                        onChange: (e) => setConsent(e.target.checked)
                      }),
                      jsx('span', { children: t('dashboard.settings.consent', { size: fmtBytes(local.expected) }) })
                    ]
                  })
                : null,
              !local.downloaded
                ? jsx(Button, {
                    size: 'sm', disabled: !consent || downloading, onClick: startDownload,
                    children: t(downloading ? 'dashboard.settings.local_downloading' : 'dashboard.settings.download')
                  })
                : null
            ]
          })
        : null,
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Button, { size: 'sm', onClick: saveEmbedding, children: t('dashboard.settings.embedding_save') }),
          jsx('span', {
            className: 'text-[0.625rem] text-(--ui-text-quaternary)',
            children: t('dashboard.settings.embedding_next_session')
          })
        ]
      }),

      ] }),
      jsx(ModelSettings, {})
      ] }),
      jsx(Separator, {}),
      jsx(MemorySettings, {}),
      jsx(Separator, {}),
      jsx(NotesSettings, {}),

    ]
  })
}

// ── About ───────────────────────────────────────────────────────────────────
// What the plugin is: name, version, license, languages. Nothing else.
function AboutView() {
  const tick = useValue(tick$)
  const { data, isLoading, error } = useQuery({
    queryKey: ['carnet', 'about', tick], queryFn: () => api('/about'), staleTime: 0
  })
  if (error) return jsx(ErrorState, { title: t('errors.backend_unreachable'), message: String(error.message || error) })
  if (isLoading && !data) return jsx(Skeleton, { className: 'h-32 w-full' })
  const languages = (data.languages || []).map((code) => t(`dashboard.settings.language_${code}`)).join(', ')
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-3 overflow-y-auto',
    children: [jsxs('div', {
      className: 'flex flex-col',
      children: [
        jsx(Row, { label: t('dashboard.about.name'), value: data.name }),
        jsx(Row, { label: t('dashboard.about.version'), value: data.version }),
        jsx(Row, { label: t('dashboard.about.license'), value: data.license }),
        data.author
          ? jsx(Row, { label: t('dashboard.about.author'), value: data.author })
          : null,
        jsx(Row, { label: t('dashboard.about.repository'), value: data.repository || t('dashboard.about.repository_soon') }),
        jsx(Row, { label: t('dashboard.about.languages'), value: languages })
      ]
    }), jsxs('div', {
      className: 'flex flex-col gap-1.5 pt-1',
      children: [
        jsx(Button, {
          size: 'sm', variant: 'outline',
          onClick: async () => {
            try {
              await api('/settings/onboarding', { method: 'POST', body: { done: false } })
              obClearDraft()
              obDraft$.set({ ...OB_DRAFT_DEFAULT })
              tab$.set('facts')
              await refresh()
            } catch (e) { notifyError(e, t('errors.settings_failed')) }
          },
          children: t('dashboard.about.restart_onboarding')
        }),
        jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: t('dashboard.about.restart_onboarding_hint') })
      ]
    })]
  })
}


// ── Injections ──────────────────────────────────────────────────────────────
// What recall really sent to the model, turn by turn, and the usage leaderboard.
// It is the only view that tells the truth about what comes out of memory.
function InjectionsView() {
  const tick = useValue(tick$)
  const { data: usage } = useQuery({
    queryKey: ['carnet', 'usage', tick],
    queryFn: () => api('/usage')
  })
  const { data: turnsData } = useQuery({
    queryKey: ['carnet', 'injections', tick],
    queryFn: () => api('/injections?limit=40')
  })

  const goTo = (id) => { search$.set(id); tab$.set('facts') }
  const list = (usage && usage.by_day) || []
  const maxi = Math.max(1, ...list.map((d) => d.n))
  const top = (usage && usage.top) || []
  const turnsList = (turnsData && turnsData.turns) || []

  return jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto',
    children: [
      jsxs('div', {
        className: 'grid grid-cols-3 gap-2',
        children: [
          jsxs('div', { className: 'rounded-md border border-(--border)/50 px-3 py-2', children: [
            jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.cumulative') }),
            jsx('div', { className: 'text-lg font-medium text-(--ui-text-primary)', children: String((usage && usage.total_uses) || 0) })
          ] }),
          jsxs('div', { className: 'rounded-md border border-(--border)/50 px-3 py-2', children: [
            jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.never') }),
            jsx('div', { className: 'text-lg font-medium text-(--ui-text-secondary)', children: String((usage && usage.never_used) || 0) })
          ] }),
          jsxs('div', { className: 'rounded-md border border-(--border)/50 px-3 py-2', children: [
            jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.traced') }),
            jsx('div', { className: 'text-lg font-medium text-(--ui-text-secondary)', children: String(turnsList.length) })
          ] })
        ]
      }),


      jsxs('div', {
        className: 'rounded-md border border-(--border)/50',
        children: [
          jsx('div', { className: 'border-b border-(--border)/40 px-3 py-1.5 text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.most_recalled') }),
          top.length
            ? top.map((f) => jsxs('button', {
                key: f.id,
                type: 'button',
                onClick: () => goTo(f.id),
                className: 'flex w-full items-center gap-2 border-b border-(--border)/25 px-3 py-1.5 text-left last:border-0 hover:bg-(--ui-bg-hover)',
                children: [
                  jsx(Badge, { variant: 'muted', children: `${f.uses}×` }),
                  jsx('span', { className: 'min-w-0 flex-1 truncate text-[0.75rem] text-(--ui-text-tertiary)', children: f.text }),
                  jsx('span', { className: 'shrink-0 font-mono text-[0.625rem] text-(--ui-text-quaternary)', children: f.id })
                ]
              }, f.id))
            : jsx('div', { className: 'px-3 py-3 text-[0.75rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.no_recall') })
        ]
      }),

      jsxs('div', {
        className: 'rounded-md border border-(--border)/50',
        children: [
          jsx('div', { className: 'border-b border-(--border)/40 px-3 py-1.5 text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.latest') }),
          turnsList.length
            ? turnsList.map((turn, i) => jsxs('div', {
                key: `${turn.ts}-${i}`,
                className: 'flex flex-col gap-1 border-b border-(--border)/25 px-3 py-2 last:border-0',
                children: [
                  jsxs('div', { className: 'flex items-center gap-2', children: [
                    jsx('span', { className: 'font-mono text-[0.625rem] text-(--ui-text-quaternary)', children: (turn.ts || '').slice(11, 19) }),
                    jsx('span', { className: 'truncate text-[0.6875rem] text-(--ui-text-tertiary)', children: turn.query || '—' }),
                    jsx('span', { className: 'ml-auto shrink-0 text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.turn_detail', { facts: (turn.ids || []).length, chars: turn.chars || 0 }) })
                  ] }),
                  (turn.ids || []).length
                    ? jsx('div', { className: 'flex flex-wrap gap-1', children: (turn.ids || []).map((id) => jsx('button', {
                        key: id, type: 'button', onClick: () => goTo(id),
                        className: 'rounded border border-(--border)/60 px-1.5 py-0.5 font-mono text-[0.625rem] text-(--ui-text-tertiary) hover:bg-(--ui-bg-hover)',
                        children: id
                      }, id)) })
                    : jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.nothing_injected') })
                ]
              }, `${turn.ts}-${i}`))
            : jsx('div', { className: 'px-3 py-3 text-[0.75rem] text-(--ui-text-quaternary)', children: t('dashboard.injections.no_turn') })
        ]
      })
    ]
  })
}


// ── Stats ───────────────────────────────────────────────────────────────────
// Evolution charts. Hand-made SVG, zero library: colours come from the theme,
// so everything follows the skin without duplicating anything.
const TINT = {
  core: 'var(--ui-accent)',
  facts: 'color-mix(in oklab, var(--ui-accent) 45%, var(--ui-text-quaternary))',
  sheets: 'color-mix(in oklab, var(--ui-accent) 15%, var(--ui-text-quaternary))',
  neutral: 'color-mix(in oklab, var(--ui-accent) 45%, transparent)',
  pale: 'color-mix(in oklab, var(--ui-accent) 28%, transparent)',
  recall: 'color-mix(in oklab, var(--ui-accent) 85%, transparent)'
}

function shortDate(j) {
  if (!j) return ''
  const [, m, d] = j.split('-')
  return `${d}/${m}`
}

function StatCard({ title, value, detail, accent }) {
  return jsxs('div', {
    className: 'flex min-w-[120px] flex-1 flex-col gap-0 rounded-md border border-(--border)/50 px-2.5 py-1.5',
    children: [
      jsx('div', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: title }),
      jsx('div', {
        className: 'font-mono text-base leading-tight',
        style: { color: accent ? 'var(--ui-accent)' : 'var(--ui-text-primary)' },
        children: value
      }),
      detail ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: detail }) : null
    ]
  })
}

function Curve({ series }) {
  const [idx, setIdx] = useState(null)
  if (series.length < 2) {
    return jsx('div', { className: 'py-6 text-center text-[0.6875rem] text-(--ui-text-quaternary)', children: t('dashboard.stats.not_enough') })
  }
  const W = 100, H = 40
  const vals = series.map((d) => d.cumulative)
  const maxi = Math.max(1, ...vals)
  const xy = vals.map((v, i) => [(i / (vals.length - 1)) * W, H - (v / maxi) * (H - 3) - 1])
  const line = 'M' + xy.map((q) => `${q[0].toFixed(2)},${q[1].toFixed(2)}`).join(' L')
  const area = `${line} L${W},${H} L0,${H} Z`
  const last = xy[xy.length - 1]

  const hover = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    const f = Math.min(1, Math.max(0, (e.clientX - r.left) / Math.max(1, r.width)))
    setIdx(Math.round(f * (series.length - 1)))
  }
  const current = idx == null ? null : series[idx]
  const point = idx == null ? null : xy[idx]

  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsxs('div', {
      className: 'relative',
      onMouseMove: hover,
      onMouseLeave: () => setIdx(null),
      children: [
        jsx('svg', {
          viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', style: { height: 196, width: '100%', display: 'block' },
          children: [
            [0.25, 0.5, 0.75].map((f) => jsx('line', {
              key: f, x1: 0, x2: W, y1: H - f * (H - 3), y2: H - f * (H - 3),
              style: { stroke: 'var(--border)', strokeWidth: 0.4, vectorEffect: 'non-scaling-stroke', opacity: 0.6 }
            }, f)),
            jsx('path', { d: area, style: { fill: 'var(--ui-accent)', opacity: 0.13 } }),
            jsx('path', {
              d: line,
              style: { fill: 'none', stroke: 'var(--ui-accent)', strokeWidth: 1, vectorEffect: 'non-scaling-stroke', strokeLinejoin: 'round' }
            }),
            point
              ? jsx('line', {
                  x1: point[0], x2: point[0], y1: 0, y2: H,
                  style: { stroke: 'var(--ui-accent)', strokeWidth: 0.5, vectorEffect: 'non-scaling-stroke', opacity: 0.7 }
                })
              : null,
            jsx('circle', {
              cx: (point || last)[0], cy: (point || last)[1],
              r: point ? 1.8 : 1.2,
              style: { fill: 'var(--ui-accent)', vectorEffect: 'non-scaling-stroke' }
            })
          ]
        }),
        current
          ? jsxs('div', {
              className: 'pointer-events-none absolute -top-1 z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded border border-(--border) bg-(--ui-bg-secondary) px-2 py-1 text-[0.625rem] leading-tight shadow-lg',
              style: { left: `${(idx / (series.length - 1)) * 100}%` },
              children: [
                jsx('div', { className: 'font-mono text-(--ui-text-secondary)', children: shortDate(current.day) }),
                jsx('div', { className: 'text-(--ui-accent)', children: t('dashboard.stats.memories_tip', { count: current.cumulative }) }),
                jsx('div', { className: 'text-(--ui-text-quaternary)', children: t('dashboard.stats.day_tip', { created: current.created, recalls: current.recalls }) })
              ]
            })
          : null
      ]
    }),
    jsxs('div', { className: 'flex justify-between font-mono text-[0.5625rem] text-(--ui-text-quaternary)', children: [
      jsx('span', { children: shortDate(series[0].day) }),
      jsx('span', { children: current ? shortDate(current.day) : t('dashboard.stats.max_tip', { count: maxi }) }),
      jsx('span', { children: shortDate(series[series.length - 1].day) })
    ] })
  ] })
}

function DayBars({ series }) {
  const [idx, setIdx] = useState(null)
  if (!series.length) return null
  const maxi = Math.max(1, ...series.map((d) => Math.max(d.created, d.recalls)))
  // Square-root scale: a peak at 399 flattened every other day to zero pixels.
  // In root, a day at 20 stays readable next to the peak.
  const heightFor = (v) => (v <= 0 ? 0 : Math.max(2, Math.sqrt(v / maxi) * 100))
  const hover = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    const f = Math.min(1, Math.max(0, (e.clientX - r.left) / Math.max(1, r.width)))
    setIdx(Math.min(series.length - 1, Math.floor(f * series.length)))
  }
  const d = idx == null ? null : series[idx]

  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsxs('div', {
      className: 'relative flex h-16 items-end gap-[2px] border-b border-(--border)/40',
      onMouseMove: hover,
      onMouseLeave: () => setIdx(null),
      children: [
        series.map((j, i) => jsxs('div', {
          key: j.day,
          className: i === idx ? 'flex h-full min-w-0 flex-1 items-end gap-[1px] rounded-sm bg-(--ui-bg-hover)' : 'flex h-full min-w-0 flex-1 items-end gap-[1px]',
          children: [
            jsx('div', { className: 'flex-1 rounded-t-sm', style: { height: `${heightFor(j.created)}%`, background: TINT.pale } }),
            jsx('div', { className: 'flex-1 rounded-t-sm', style: { height: `${heightFor(j.recalls)}%`, background: TINT.recall } })
          ]
        }, j.day)),
        d
          ? jsxs('div', {
              className: 'pointer-events-none absolute -top-1 z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded border border-(--border) bg-(--ui-bg-secondary) px-2 py-1 text-[0.625rem] leading-tight shadow-lg',
              style: { left: `${((idx + 0.5) / series.length) * 100}%` },
              children: [
                jsx('div', { className: 'font-mono text-(--ui-text-secondary)', children: shortDate(d.day) }),
                jsx('div', { style: { color: TINT.recall }, children: t('dashboard.stats.bar_tip', { count: d.recalls }) }),
                jsx('div', { className: 'text-(--ui-text-quaternary)', children: t('dashboard.stats.total_tip', { created: d.created, total: d.cumulative }) })
              ]
            })
          : null
      ]
    }),
    jsxs('div', { className: 'flex justify-between font-mono text-[0.5625rem] text-(--ui-text-quaternary)', children: [
      jsx('span', { children: shortDate(series[0].day) }),
      jsx('span', { children: d ? shortDate(d.day) : t('dashboard.stats.peak_tip', { count: maxi }) }),
      jsx('span', { children: shortDate(series[series.length - 1].day) })
    ] })
  ] })
}

function Donut({ parts }) {
  const total = parts.reduce((a, p) => a + p.n, 0) || 1
  const R = 15.5, C = 2 * Math.PI * R
  let cursor = 0
  return jsxs('svg', {
    viewBox: '0 0 40 40', className: 'h-24 w-24 shrink-0 -rotate-90',
    children: [
      jsx('circle', { cx: 20, cy: 20, r: R, fill: 'none', strokeWidth: 5.5, style: { stroke: 'var(--border)', opacity: 0.35 } }),
      ...parts.filter((p) => p.n > 0).map((p) => {
        const frac = p.n / total
        const el = jsx('circle', {
          cx: 20, cy: 20, r: R, fill: 'none', strokeWidth: 5.5, strokeLinecap: 'butt',
          style: { stroke: p.color, strokeDasharray: `${(frac * C - 0.6).toFixed(2)} ${C.toFixed(2)}`, strokeDashoffset: (-cursor * C).toFixed(2) }
        }, p.name)
        cursor += frac
        return el
      })
    ]
  })
}

function Legend({ name, n, total, color }) {
  const frac = total ? Math.round((n / total) * 100) : 0
  return jsxs('div', { className: 'flex items-center gap-2 text-[0.6875rem]', children: [
    jsx('span', { className: 'h-2 w-2 shrink-0 rounded-sm', style: { background: color } }),
    jsx('span', { className: 'min-w-0 flex-1 truncate text-(--ui-text-tertiary)', children: name }),
    jsx('span', { className: 'font-mono text-[0.625rem] text-(--ui-text-quaternary)', children: `${n}` }),
    jsx('span', { className: 'w-8 text-right font-mono text-[0.625rem] text-(--ui-text-quaternary)', children: `${frac}%` })
  ] })
}

function HBar({ name, n, total, color }) {
  const frac = total ? Math.round((n / total) * 100) : 0
  // The label keeps a fixed share of the width: a 90% bar must not crush a
  // 6-point text.
  return jsxs('div', {
    className: 'grid items-center gap-2',
    style: { gridTemplateColumns: 'minmax(0, 44%) 1fr auto' },
    children: [
      jsx('span', { className: 'truncate text-[0.75rem] text-(--ui-text-secondary)', title: name, children: name }),
      jsx('div', { className: 'h-1 overflow-hidden rounded-full bg-(--ui-text-quaternary)/15', children:
        jsx('div', { className: 'h-full rounded-full', style: { width: `${Math.max(2, frac)}%`, background: color || TINT.recall } })
      }),
      jsx('span', { className: 'w-8 text-right font-mono text-[0.6875rem] text-(--ui-text-quaternary)', children: String(n) })
    ]
  })
}

function Histogram({ sizes }) {
  const [idx, setIdx] = useState(null)
  const labels = ['<100', '100-200', '200-300', '300-400', '400-500', '500+']
  const maxi = Math.max(1, ...sizes)
  const hover = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    const f = Math.min(1, Math.max(0, (e.clientX - r.left) / Math.max(1, r.width)))
    setIdx(Math.min(sizes.length - 1, Math.floor(f * sizes.length)))
  }
  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsxs('div', {
      className: 'relative flex items-end gap-1 border-b border-(--border)/40',
      style: { height: 168 },
      onMouseMove: hover,
      onMouseLeave: () => setIdx(null),
      children: [
        sizes.map((n, i) => jsx('div', {
          key: i,
          className: i === idx ? 'flex-1 rounded-t-sm bg-(--ui-bg-hover)' : 'flex-1 rounded-t-sm',
          style: {
            height: `${Math.max(2, Math.sqrt(n / maxi) * 100)}%`,
            background: TINT.recall
          }
        }, i)),
        idx != null && sizes[idx] != null
          ? jsxs('div', {
              className: 'pointer-events-none absolute -top-1 z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded border border-(--border) bg-(--ui-bg-secondary) px-2 py-1 text-[0.625rem] leading-tight shadow-lg',
              style: { left: `${((idx + 0.5) / sizes.length) * 100}%` },
              children: [
                jsx('div', { className: 'font-mono text-(--ui-text-secondary)', children: t('dashboard.stats.range_tip', { range: labels[idx] }) }),
                jsx('div', { className: 'text-(--ui-text-quaternary)', children: t('dashboard.stats.count_tip', { count: sizes[idx] }) })
              ]
            })
          : null
      ]
    }),
    jsxs('div', { className: 'flex gap-1 font-mono text-[0.5rem] text-(--ui-text-quaternary)', children:
      labels.map((e) => jsx('span', { key: e, className: 'flex-1 text-center', children: e }, e))
    })
  ] })
}

function Hours({ hours }) {
  const [idx, setIdx] = useState(null)
  const maxi = Math.max(1, ...hours)
  const hover = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    const f = Math.min(1, Math.max(0, (e.clientX - r.left) / Math.max(1, r.width)))
    setIdx(Math.min(23, Math.floor(f * 24)))
  }
  return jsxs('div', { className: 'flex flex-col gap-1', children: [
    jsxs('div', {
      className: 'relative flex items-end gap-[2px] border-b border-(--border)/40',
      style: { height: 168 },
      onMouseMove: hover,
      onMouseLeave: () => setIdx(null),
      children: [
        hours.map((n, h) => jsx('div', {
          key: h,
          className: h === idx ? 'flex-1 rounded-t-sm bg-(--ui-bg-hover)' : 'flex-1 rounded-t-sm',
          style: { height: `${Math.max(1.5, Math.sqrt(n / maxi) * 100)}%`, background: n ? TINT.recall : 'var(--ui-text-quaternary)', opacity: n ? 1 : 0.2 }
        }, h)),
        idx != null && hours[idx]
          ? jsx('div', {
              className: 'pointer-events-none absolute -top-1 z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded border border-(--border) bg-(--ui-bg-secondary) px-2 py-1 font-mono text-[0.625rem] leading-tight text-(--ui-text-secondary) shadow-lg',
              style: { left: `${((idx + 0.5) / 24) * 100}%` },
              children: t('dashboard.stats.hour_tip', { hour: String(idx).padStart(2, '0'), count: hours[idx] })
            })
          : null
      ]
    }),
    jsxs('div', { className: 'flex justify-between font-mono text-[0.5625rem] text-(--ui-text-quaternary)', children: [
      jsx('span', { children: '00h' }), jsx('span', { children: '12h' }), jsx('span', { children: '23h' })
    ] })
  ] })
}

function Panel({ title, sub, children, style }) {
  return jsxs('div', {
    style,
    className: 'flex min-w-0 flex-col gap-1.5 rounded-md border border-(--border)/50 px-2.5 py-2',
    children: [
      jsxs('div', { className: 'flex items-baseline justify-between gap-2', children: [
        jsx('span', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: title }),
        sub ? jsx('span', { className: 'font-mono text-[0.625rem] text-(--ui-text-quaternary)', children: sub }) : null
      ] }),
      children
    ]
  })
}

function StatsView() {
  const tick = useValue(tick$)
  const { data } = useQuery({ queryKey: ['carnet', 'graphs', tick], queryFn: () => api('/graphs') })
  if (!data) return jsx(Skeleton, { className: 'h-64 w-full' })

  const series = data.series || []
  const layers = data.layers || {}
  const core = Number(layers['0']) || 0
  const facts = Number(layers['1']) || 0
  const sheets = Number(layers['2']) || 0
  const total = core + facts + sheets
  const cov = data.coverage || { used: 0, never: 0 }
  const recalls = series.reduce((a, d) => a + d.recalls, 0)
  const covered = total ? Math.round((cov.used / total) * 100) : 0
  const certain = (data.confidence && data.confidence.certain) || 0
  const probable = (data.confidence && data.confidence.probable) || 0
  const topics = data.topics || []
  const words = data.keywords || []
  const topTopic = topics.length ? topics[0].n : 1
  const topWord = words.length ? words[0].n : 1

  const column = 'grid min-w-0 grid-cols-2 gap-2.5'

  return jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto pr-1',
    children: [
      jsxs('div', { className: 'grid grid-cols-4 gap-2', children: [
        jsx(StatCard, { title: t('dashboard.stats.memories'), value: String(total), detail: t('dashboard.stats.memories_detail', { active: facts + sheets, core }) }),
        jsx(StatCard, { title: t('dashboard.stats.recalls'), value: String(recalls), detail: t('dashboard.stats.touched', { percent: covered }), accent: true }),
        jsx(StatCard, { title: t('dashboard.stats.never_recalled'), value: String(cov.never), detail: t('dashboard.stats.never_detail', { percent: 100 - covered }) }),
        jsx(StatCard, {
          title: t('dashboard.stats.length'),
          value: `${(data.averages && data.averages['1']) || data.average_length || 0}`,
          detail: t('dashboard.stats.length_detail', {
            core: (data.averages && data.averages['0']) || 0,
            sheet: (data.averages && data.averages['2']) || 0
          })
        })
      ] }),

      jsxs('div', { className: column, children: [        jsx(Panel, { title: t('dashboard.stats.layers'), sub: `${total}`, children: jsxs('div', { className: 'flex items-center gap-3', children: [
            jsx(Donut, { parts: [
              { name: 'core', n: core, color: TINT.core },
              { name: 'facts', n: facts, color: TINT.facts },
              { name: 'sheets', n: sheets, color: TINT.sheets }
            ] }),
            jsxs('div', { className: 'flex w-[200px] min-w-0 shrink-0 flex-col gap-1.5', children: [
              jsx(Legend, { name: t('dashboard.tabs.core'), n: core, total, color: TINT.core }),
              jsx(Legend, { name: t('dashboard.tabs.facts'), n: facts, total, color: TINT.facts }),
              jsx(Legend, { name: t('dashboard.filters.sheets'), n: sheets, total, color: TINT.sheets })
            ] })
          ] })
        }),
        jsx(Panel, {
          title: t('dashboard.stats.confidence'), sub: `${certain + probable}`,
          children: jsxs('div', { className: 'flex items-center gap-3', children: [
            jsx(Donut, { parts: [
              { name: 'certain', n: certain, color: TINT.core },
              { name: 'probable', n: probable, color: TINT.sheets }
            ] }),
            jsxs('div', { className: 'flex w-[200px] min-w-0 shrink-0 flex-col gap-1.5', children: [
              jsx(Legend, { name: t('dashboard.stats.certain'), n: certain, total: certain + probable, color: TINT.core }),
              jsx(Legend, { name: t('dashboard.stats.probable'), n: probable, total: certain + probable, color: TINT.sheets })
            ] })
          ] })
        })
      ] }),

      jsxs('div', { className: 'grid min-w-0 grid-cols-2 items-start gap-2.5', children: [
        jsx('div', { className: 'min-w-0', style: { height: 268 }, children: jsx(Panel, { title: t('dashboard.stats.cumulative'), sub: t('dashboard.stats.days', { count: series.length }), style: { height: '100%' }, children: jsx(Curve, { series }) }) }),
        jsx('div', { className: 'min-w-0', style: { height: 268 }, children: jsx(Panel, { title: t('dashboard.stats.topics'), sub: t('dashboard.stats.top'), style: { height: '100%', overflow: 'hidden' }, children: jsxs('div', { className: 'flex flex-col gap-1.5', children:
          topics.slice(0, 10).map((x) => jsx(HBar, { name: x.topic, n: x.n, total: topTopic, color: TINT.recall }, x.topic))
        }) }) })
      ] }),

      jsxs('div', { className: 'grid min-w-0 grid-cols-2 items-start gap-2.5', children: [
        jsx('div', { className: 'min-w-0', children: jsx(Panel, { title: t('dashboard.stats.length_panel'), sub: t('dashboard.stats.chars'), children: jsx(Histogram, { sizes: data.sizes || [] }) }) }),
        jsx('div', { className: 'min-w-0', children: jsx(Panel, { title: t('dashboard.stats.hours'), sub: t('dashboard.stats.hours_range'), children: jsx(Hours, { hours: data.by_hour || [] }) }) })
      ] }),

    ]
  })
}

// ── Memory graph (ported from the starmap renderer) ───────────────
var Starmap = (() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, { get: all[name], enumerable: true });
  };
  var __copyProps = (to, from, except, desc) => {
    if (from && typeof from === "object" || typeof from === "function") {
      for (let key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(to, key) && key !== except)
          __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
    }
    return to;
  };
  var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

  // entry.ts
  var entry_exports = {};
  __export(entry_exports, {
    NODE_SHAPE: () => NODE_SHAPE,
    RING_INNER: () => RING_INNER,
    RING_OUTER: () => RING_OUTER,
    TILT: () => TILT,
    ZOOM_MAX: () => ZOOM_MAX,
    ZOOM_MIN: () => ZOOM_MIN,
    clamp: () => clamp,
    computePalette: () => computePalette,
    darken: () => darken,
    drawScene: () => drawScene,
    drawScramble: () => drawScramble,
    fitViewport: () => fitViewport,
    mixRgb: () => mixRgb,
    nodeRadius: () => nodeRadius,
    radiusForRecency: () => radiusForRecency,
    recencyInk: () => recencyInk,
    rgba: () => rgba
  });

  // src/app/starmap/constants.ts
  var RING_INNER = 58;
  var RING_OUTER = 340;
  var ZOOM_MIN = 0.3;
  var ZOOM_MAX = 5;
  var FIT_PADDING = 80;
  var TILT = 1;
  var WHITE = { b: 255, g: 255, r: 255 };
  var BLACK = { b: 0, g: 0, r: 0 };
  var AGE_GRADIENT = { mid: 0.52, midInk: 0.74, newInk: 0.95, oldInk: 0.42, reach: 1 };
  var NODE_SHAPE = { memory: "diamond", skill: "circle" };
  var ORB_DARKEN = 0.3;
  var WHITEISH_SHEEN = 0.95;
  var LIT_BAND_ALPHA = 0.04;
  var MODE_DEFAULTS = {
    dark: {
      lineAlpha: 0.24,
      lineDash: 1.5,
      lineDashed: true,
      lineWidth: 0.5,
      ringAlpha: 0.1,
      ringDash: 4,
      ringDashed: false,
      ringWidth: 1.5
    },
    light: {
      lineAlpha: 0.18,
      lineDash: 1.5,
      lineDashed: true,
      lineWidth: 0.5,
      ringAlpha: 0.06,
      ringDash: 4,
      ringDashed: false,
      ringWidth: 2
    }
  };
  var RING_PARAMS = {
    dark: { bandAlpha: 0.01, lightSize: 0.64, ringAlpha: 0.06, sheen: 0.12 },
    light: { bandAlpha: 0.03, lightSize: 0.27, ringAlpha: 0.028, sheen: 0.1 }
  };

  // src/app/starmap/geometry.ts
  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }
  function nodeRadius(n) {
    if (n.kind === "memory") {
      return 4.4;
    }
    const base = n.state === "archived" || n.state === "stale" ? 2.4 : 3;
    return base + Math.sqrt(Math.max(0, n.useCount)) * 0.55 + (n.pinned ? 0.8 : 0);
  }
  function recencyInk(rec) {
    const reach = Math.max(0.01, AGE_GRADIENT.reach);
    const mid = clamp(AGE_GRADIENT.mid, 0.01, 0.99);
    const t = clamp(rec / reach, 0, 1);
    if (t <= mid) {
      const p2 = t / mid;
      return AGE_GRADIENT.oldInk + (AGE_GRADIENT.midInk - AGE_GRADIENT.oldInk) * (p2 * p2 * (3 - 2 * p2));
    }
    const p = (t - mid) / (1 - mid);
    return AGE_GRADIENT.midInk + (AGE_GRADIENT.newInk - AGE_GRADIENT.midInk) * (p * p * (3 - 2 * p));
  }
  function shapePath(ctx, shape, x, y, r) {
    ctx.beginPath();
    if (shape === "square") {
      ctx.rect(x - r, y - r, r * 2, r * 2);
      return;
    }
    if (shape === "circle") {
      ctx.arc(x, y, r, 0, Math.PI * 2);
      return;
    }
    const pts = shape === "diamond" ? 4 : shape === "triangle" ? 3 : 6;
    const rot = shape === "hexagon" ? Math.PI / 6 : -Math.PI / 2;
    for (let i = 0; i < pts; i += 1) {
      const a = rot + i / pts * Math.PI * 2;
      const px = x + Math.cos(a) * r;
      const py = y + Math.sin(a) * r;
      if (i === 0) {
        ctx.moveTo(px, py);
      } else {
        ctx.lineTo(px, py);
      }
    }
    ctx.closePath();
  }
  function fitViewport(w, h, outer = RING_OUTER) {
    if (w <= 0 || h <= 0) {
      return { k: 1, x: w / 2, y: h / 2 };
    }
    const kFor = (r) => {
      const spanX = (r + 30) * 2;
      return Math.min((w - FIT_PADDING * 2) / spanX, (h - FIT_PADDING * 2) / (spanX * TILT), 2.2);
    };
    const k = clamp(Math.max(kFor(outer), kFor(RING_OUTER)), ZOOM_MIN, ZOOM_MAX);
    return { k, x: w / 2, y: h / 2 + h * 0.05 };
  }
  function radiusForRecency(rec, outer = RING_OUTER) {
    return RING_INNER + rec * (outer - RING_INNER);
  }
  var fitScale = (w, h, rings) => fitViewport(w, h, rings.at(-1)?.r ?? RING_OUTER).k;

  // src/app/starmap/color.ts
  var _probe = null;
  function resolveRgb(color) {
    if (!_probe) {
      const c = document.createElement("canvas");
      c.width = 1;
      c.height = 1;
      _probe = c.getContext("2d", { willReadFrequently: true });
    }
    if (!_probe) {
      return { b: 184, g: 163, r: 148 };
    }
    _probe.clearRect(0, 0, 1, 1);
    _probe.fillStyle = "#888888";
    _probe.fillStyle = color;
    _probe.fillRect(0, 0, 1, 1);
    const d = _probe.getImageData(0, 0, 1, 1).data;
    return { b: d[2], g: d[1], r: d[0] };
  }
  function rgba(c, a) {
    return `rgba(${c.r},${c.g},${c.b},${a})`;
  }
  function mixRgb(a, b, t) {
    const p = clamp(t, 0, 1);
    return {
      b: Math.round(a.b + (b.b - a.b) * p),
      g: Math.round(a.g + (b.g - a.g) * p),
      r: Math.round(a.r + (b.r - a.r) * p)
    };
  }
  function darken(c, amount) {
    return mixRgb(c, BLACK, amount);
  }
  function luminance(r, g, b) {
    return (0.2126 * r + 0.7152 * g + 0.114 * b) / 255;
  }
  function rgbToHsl(c) {
    const r = c.r / 255;
    const g = c.g / 255;
    const b = c.b / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const l = (max + min) / 2;
    const d = max - min;
    let h = 0;
    let s = 0;
    if (d) {
      s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
      h = (max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4) * 60;
    }
    return [h, s, l];
  }
  function hslToRgb(h, s, l) {
    const hue = (h % 360 + 360) % 360;
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs(hue / 60 % 2 - 1));
    const m = l - c / 2;
    const [r, g, b] = hue < 60 ? [c, x, 0] : hue < 120 ? [x, c, 0] : hue < 180 ? [0, c, x] : hue < 240 ? [0, x, c] : hue < 300 ? [x, 0, c] : [c, 0, x];
    return { b: Math.round((b + m) * 255), g: Math.round((g + m) * 255), r: Math.round((r + m) * 255) };
  }
  function complementaryInk(c) {
    const [h, s, l] = rgbToHsl(c);
    return hslToRgb(h + 165, Math.max(s, 0.5), clamp(l, 0.5, 0.7));
  }
  function memoryInkFor(primary, bg) {
    return mixRgb(complementaryInk(primary), bg, 0.45);
  }
  function computePalette(canvas) {
    const style = getComputedStyle(canvas);
    const fg = resolveRgb(style.color);
    const darkTheme = luminance(fg.r, fg.g, fg.b) > 0.55;
    const base = darkTheme ? { b: 255, g: 255, r: 255 } : { b: 0, g: 0, r: 0 };
    const primary = resolveRgb(style.getPropertyValue("--theme-primary").trim() || style.color);
    const bg = resolveRgb(
      style.getPropertyValue("--background").trim() || style.getPropertyValue("--dt-background").trim() || (darkTheme ? "#000" : "#fff")
    );
    return {
      // Band tint derives from the theme primary so rings read consistently in
      // both modes (foreground ink would go white on dark / black on light).
      bandInk: mixRgb(primary, base, darkTheme ? 0.3 : 0),
      base,
      bg,
      c: MODE_DEFAULTS[darkTheme ? "dark" : "light"],
      chipBg: darkTheme ? "rgba(0,0,0,0.72)" : "rgba(255,255,255,0.85)",
      darkTheme,
      inkInv: darkTheme ? "rgba(0,0,0,1)" : "rgba(255,255,255,1)",
      memoryInk: memoryInkFor(primary, bg),
      primary,
      skillInk: mixRgb(primary, base, darkTheme ? 0.12 : 0.18)
    };
  }

  // src/lib/time.ts
  var fmtClock = new Intl.DateTimeFormat(void 0, { hour: "numeric", minute: "2-digit" });
  var fmtDayTime = new Intl.DateTimeFormat(void 0, {
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    month: "short"
  });
  var fmtDateTime = new Intl.DateTimeFormat(void 0, { dateStyle: "medium", timeStyle: "short" });
  var fmtFullDateTime = new Intl.DateTimeFormat(void 0, { dateStyle: "full", timeStyle: "short" });
  var fmtDate = new Intl.DateTimeFormat(void 0, { day: "numeric", month: "short", year: "numeric" });
  var fmtMonth = new Intl.DateTimeFormat(void 0, { month: "long" });
  var fmtMonthYear = new Intl.DateTimeFormat(void 0, { month: "long", year: "numeric" });
  var rtf = new Intl.RelativeTimeFormat(void 0, { numeric: "auto", style: "short" });

  // src/app/starmap/text.ts
  function formatDate(ts) {
    if (!ts) {
      return "unknown";
    }
    try {
      return fmtDate.format(new Date(ts * 1e3));
    } catch {
      return "unknown";
    }
  }
  function metaBadges(n) {
    const out = [formatDate(n.timestamp)];
    if (n.kind === "memory") {
      out.push(n.memorySource === "profile" ? "profile memory" : "memory");
    } else {
      out.push(n.category);
      if (n.createdBy === "agent" || n.createdBy === "learn") {
        out.push("learned");
      }
      if (n.pinned) {
        out.push("pinned");
      }
    }
    return out.filter(Boolean);
  }
  function countLabel(n) {
    return n.kind === "skill" && n.useCount > 0 ? `x${n.useCount}` : null;
  }
  function nodeFooter(node) {
    void node;
    return null;
  }
  function wrapText(ctx, text, maxW) {
    const words = text.split(/\s+/).filter(Boolean);
    const lines = [];
    let line = "";
    for (const word of words) {
      const next = line ? `${line} ${word}` : word;
      if (!line || ctx.measureText(next).width <= maxW) {
        line = next;
      } else {
        lines.push(line);
        line = word;
      }
    }
    if (line) {
      lines.push(line);
    }
    return lines;
  }
  function ellipsize(ctx, text, maxW) {
    if (ctx.measureText(text).width <= maxW) {
      return text;
    }
    let s = text;
    while (s.length > 1 && ctx.measureText(`${s}\u2026`).width > maxW) {
      s = s.slice(0, -1);
    }
    return `${s.trimEnd()}\u2026`;
  }

  // src/app/starmap/render.ts
  var ease = (t) => {
    const u = t < 0 ? 0 : t > 1 ? 1 : t;
    return u * u * (3 - 2 * u);
  };
  var WARP_FROM = 0.32;
  var warpIn = (t) => {
    const u = t < 0 ? 0 : t > 1 ? 1 : t;
    return u >= 1 ? 1 : 1 - 2 ** (-9 * u);
  };
  var RING_BIRTH = { down: 0.055, up: 0.032 };
  var NODE_BIRTH = { down: 0.11, up: 0.075 };
  var SCRAMBLE_CHARS = "\uFF8A\uFF8B\uFF8C\uFF8D\uFF8E\uFF8F\uFF90\uFF91\uFF92\uFF93\uFF94\uFF95\uFF96\uFF97\uFF98\uFF99\uFF9A\uFF9C\uFF9D\uFF66\uFF71\uFF73\uFF74\uFF75\uFF76\uFF77\uFF79\uFF7A\uFF7B\uFF7C\uFF7D\uFF7E\uFF80\uFF81\uFF82\uFF83\uFF85\uFF86\uFF87\uFF880123456789:.=*+<>\u039E\u2573";
  var SPRITE_R = 80;
  var spriteCache = /* @__PURE__ */ new Map();
  function sphereSprite(ink, strength, bodyDarken) {
    const key = `${ink.r},${ink.g},${ink.b}|${strength}|${bodyDarken}`;
    const cached = spriteCache.get(key);
    if (cached) {
      return cached;
    }
    const R = SPRITE_R;
    const pad = Math.ceil(R * 0.15) + 1;
    const size = (R + pad) * 2;
    const c = R + pad;
    const cv = document.createElement("canvas");
    cv.width = size;
    cv.height = size;
    const g2 = cv.getContext("2d");
    if (!g2) {
      return cv;
    }
    const mx = Math.max(ink.r, ink.g, ink.b);
    const mn = Math.min(ink.r, ink.g, ink.b);
    const sat = mx ? (mx - mn) / mx : 0;
    const whiteness = clamp((luminance(ink.r, ink.g, ink.b) - 0.7) / 0.3, 0, 1) * (1 - sat);
    const eff = strength + (WHITEISH_SHEEN - strength) * whiteness;
    const hi = mixRgb(ink, WHITE, 0.7 * eff);
    const body = darken(ink, bodyDarken * (1 - whiteness));
    const grad = g2.createRadialGradient(c - R * 0.35, c - R * 0.4, R * 0.05, c, c, R * 1.15);
    grad.addColorStop(0, rgba(hi, 1));
    grad.addColorStop(0.5, rgba(body, 1));
    grad.addColorStop(1, rgba(body, 0.85));
    g2.fillStyle = grad;
    g2.beginPath();
    g2.arc(c, c, R, 0, Math.PI * 2);
    g2.fill();
    spriteCache.set(key, cv);
    return cv;
  }
  function sphereFill(ctx, x, y, r, ink, strength, bodyDarken) {
    const sprite = sphereSprite(ink, strength, bodyDarken);
    const scale = r / SPRITE_R;
    const drawSize = sprite.width * scale;
    ctx.drawImage(sprite, x - drawSize / 2, y - drawSize / 2, drawSize, drawSize);
  }
  var rectsOverlap = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  function drawScene(scene) {
    const {
      adjacency,
      byId,
      ctx,
      dpr,
      fades,
      focusId,
      hoverId,
      hoverLink,
      hoverRing,
      links,
      memById,
      nodes,
      palette,
      reveal,
      rings,
      selectedRing,
      size,
      snapMotion = false,
      vp
    } = scene;
    const seen = (rec) => rec <= reveal + 1e-3;
    let frontier = 0;
    for (const fn of nodes) {
      if (fn.rec <= reveal + 1e-3 && fn.rec > frontier) {
        frontier = fn.rec;
      }
    }
    const erec = (rec) => frontier > 0 ? clamp(rec / frontier, 0, 1) : 1;
    const { h, w } = size;
    const { bandInk, base, bg, c, chipBg, darkTheme, inkInv, memoryInk, skillInk } = palette;
    const { bandAlpha, lightSize, ringAlpha, sheen } = RING_PARAMS[darkTheme ? "dark" : "light"];
    let animating = false;
    const ringLabelRects = [];
    const fadeAlpha = (bucket, key, target, snapUp = false, rates) => {
      const targetAlpha = clamp(target, 0, 1);
      const prev = bucket.get(key);
      if (snapMotion) {
        bucket.set(key, targetAlpha);
        return targetAlpha;
      }
      if (prev == null || snapUp && targetAlpha > prev) {
        bucket.set(key, targetAlpha);
        return targetAlpha;
      }
      const up = rates?.up ?? 0.22;
      const down = rates?.down ?? 0.32;
      const rate = targetAlpha > prev ? up : down;
      const next = prev + (targetAlpha - prev) * rate;
      if (Math.abs(next - targetAlpha) < 0.01) {
        bucket.set(key, targetAlpha);
        return targetAlpha;
      }
      animating = true;
      bucket.set(key, next);
      return next;
    };
    const shade = (a) => `rgba(${base.r},${base.g},${base.b},${a})`;
    const projX = (wx) => wx * vp.k + vp.x;
    const projY = (wy) => wy * vp.k * TILT + vp.y;
    const nodeK = fitScale(w, h, rings);
    const focusSet = focusId ? adjacency.get(focusId) ?? /* @__PURE__ */ new Set() : null;
    const ringIdx = selectedRing;
    const ring = ringIdx != null ? rings[ringIdx] ?? null : null;
    const ringLo = ring && ringIdx != null ? (rings[ringIdx - 1]?.ratio ?? 0) - 1e-3 : 0;
    const ringHi = ring ? ring.ratio + 1e-3 : 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.globalAlpha = 1;
    ctx.setTransform(vp.k * dpr, 0, 0, vp.k * TILT * dpr, vp.x * dpr, vp.y * dpr);
    const litRingIdx = hoverRing ?? ringIdx;
    const ringSeen = (i) => {
      const threshold = rings[i - 1]?.ratio ?? 0;
      return i === 0 || (threshold <= 0 ? reveal > 1e-3 : reveal + 1e-3 >= threshold);
    };
    const ringAppear = rings.map(
      (rg, i) => ease(fadeAlpha(fades.appear, `ring:${i}`, ringSeen(i) ? 1 : 0, false, RING_BIRTH))
    );
    const ringDrawR = rings.map((rg, i) => {
      const startR = ringSeen(i) ? rings[i - 1]?.r ?? rg.r : RING_INNER;
      return startR + (rg.r - startR) * (ringAppear[i] ?? 1);
    });
    const ringVis = ringAppear.map((a) => clamp(a / 0.55, 0, 1));
    if (bandAlpha > 0 || litRingIdx != null) {
      for (let i = 0; i < rings.length - 1; i += 1) {
        const lit = litRingIdx != null && i + 1 === litRingIdx;
        if (!lit && bandAlpha <= 0) {
          continue;
        }
        if ((ringAppear[i + 1] ?? 1) <= 0.01) {
          continue;
        }
        const inner = ringDrawR[i] ?? 0;
        const outer = ringDrawR[i + 1] ?? 0;
        if (lit) {
          ctx.fillStyle = rgba(bandInk, LIT_BAND_ALPHA);
        } else {
          const grad = ctx.createRadialGradient(0, 0, inner, 0, 0, outer);
          if (darkTheme) {
            grad.addColorStop(0, rgba(bandInk, 0));
            grad.addColorStop(clamp(1 - lightSize, 0.01, 0.99), rgba(bandInk, 0));
            grad.addColorStop(1, rgba(bandInk, bandAlpha));
          } else {
            grad.addColorStop(0, rgba(bandInk, bandAlpha));
            grad.addColorStop(clamp(lightSize, 0.01, 0.99), rgba(bandInk, 0));
            grad.addColorStop(1, rgba(bandInk, 0));
          }
          ctx.fillStyle = grad;
        }
        ctx.beginPath();
        ctx.arc(0, 0, outer, 0, Math.PI * 2);
        ctx.arc(0, 0, inner, 0, Math.PI * 2, true);
        ctx.fill();
      }
    }
    ctx.lineWidth = c.ringWidth / vp.k;
    ctx.setLineDash(c.ringDashed ? [c.ringDash / vp.k, c.ringDash / vp.k] : []);
    rings.forEach((rg, i) => {
      const emphasized = ringIdx != null && (i === ringIdx || i === ringIdx - 1);
      const emphasisAlpha = emphasized ? clamp(LIT_BAND_ALPHA * 2, 0, 1) : ringAlpha;
      const coreFade = i === 0 ? clamp(reveal / 0.08, 0, 1) : 1;
      const ringAlphaNow = fadeAlpha(fades.rings, String(i), emphasisAlpha, emphasized) * (ringVis[i] ?? 1) * coreFade;
      if (ringAlphaNow < 4e-3) {
        return;
      }
      ctx.strokeStyle = shade(ringAlphaNow);
      ctx.beginPath();
      ctx.arc(0, 0, ringDrawR[i] ?? rg.r, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const focusNode = focusId ? byId.get(focusId) ?? null : null;
    const focusRingR = focusNode ? nodeRadius(focusNode) * nodeK + 4 : 0;
    for (const link of links) {
      const s = typeof link.source === "object" ? link.source : byId.get(String(link.source));
      const t = typeof link.target === "object" ? link.target : byId.get(String(link.target));
      if (!s || !t) {
        continue;
      }
      const revealed = seen(s.rec) && seen(t.rec);
      const lit = revealed && !!focusId && (s.id === focusId || t.id === focusId || !!focusSet && focusSet.has(s.id) && focusSet.has(t.id));
      let x1 = projX(s.x);
      let y1 = projY(s.y);
      let x2 = projX(t.x);
      let y2 = projY(t.y);
      if (s.id === focusId) {
        const d = Math.hypot(x2 - x1, y2 - y1) || 1;
        x1 += (x2 - x1) / d * focusRingR;
        y1 += (y2 - y1) / d * focusRingR;
      }
      if (t.id === focusId) {
        const d = Math.hypot(x1 - x2, y1 - y2) || 1;
        x2 += (x1 - x2) / d * focusRingR;
        y2 += (y1 - y2) / d * focusRingR;
      }
      const key = `${s.id}->${t.id}`;
      const ambient = recencyInk(erec((s.rec + t.rec) / 2)) * c.lineAlpha;
      const targetAlpha = !revealed ? 0 : lit ? 1 : key === hoverLink ? clamp(ambient * 2, 0, 0.7) : focusId || ring ? 0.025 : ambient;
      const linkAlpha = fadeAlpha(fades.links, key, targetAlpha, lit);
      if (linkAlpha < 4e-3) {
        continue;
      }
      ctx.strokeStyle = shade(linkAlpha);
      ctx.setLineDash(lit || !c.lineDashed ? [] : [c.lineDash, c.lineDash]);
      ctx.lineWidth = lit ? 1.5 : c.lineWidth;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    const revealedRings = /* @__PURE__ */ new Set();
    for (const n of nodes) {
      const landLaid = (ringAppear[n.outerRingIndex] ?? 1) >= 0.5;
      const revealed = seen(n.rec) && landLaid;
      if (revealed) {
        revealedRings.add(n.outerRingIndex);
      }
      const isFocus = revealed && n.id === focusId;
      const isNeighbor = revealed && !!focusSet && focusSet.has(n.id);
      const inRing = !!ring && n.rec >= ringLo && n.rec < ringHi;
      const nodeHigh = isFocus || isNeighbor;
      const er = erec(n.rec);
      const ageScale = 1;
      const r = nodeRadius(n) * nodeK * ageScale;
      const baseAlpha = nodeHigh ? 1 : ring ? inRing ? focusId ? 0.55 : 1 : 0.16 : focusId ? 0.16 : recencyInk(er);
      const alpha = fadeAlpha(fades.nodes, n.id, revealed ? baseAlpha : 0, nodeHigh || inRing);
      const rawBorn = fadeAlpha(fades.appear, n.id, revealed ? 1 : 0, nodeHigh || inRing, NODE_BIRTH);
      const born = ease(rawBorn);
      const vis = alpha * born;
      if (vis < 4e-3) {
        continue;
      }
      const posScale = WARP_FROM + (1 - WARP_FROM) * warpIn(rawBorn);
      const sx = projX(n.x * posScale);
      const sy = projY(n.y * posScale);
      ctx.globalAlpha = vis;
      const nodeInk = nodeHigh ? base : n.kind === "memory" ? memoryInk : skillInk;
      const shape = NODE_SHAPE[n.kind];
      if (shape === "circle") {
        sphereFill(ctx, sx, sy, r, nodeInk, sheen, nodeHigh ? 0 : ORB_DARKEN);
      } else {
        shapePath(ctx, shape, sx, sy, r);
        ctx.fillStyle = rgba(nodeInk, 1);
        ctx.fill();
      }
      if (isFocus) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = rgba(nodeInk, 1);
        ctx.lineWidth = 1.4;
        shapePath(ctx, shape, sx, sy, r + 4);
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "center";
    const LABEL_GAP = 15;
    let lastLabelY = Number.POSITIVE_INFINITY;
    rings.forEach((rg, i) => {
      if (!rg.label || !revealedRings.has(i)) {
        return;
      }
      const sx = projX(0);
      const sy = projY(-(ringDrawR[i] ?? rg.r));
      if (sy < 8 || sy > h - 8 || lastLabelY - sy < LABEL_GAP) {
        return;
      }
      lastLabelY = sy;
      const tw = ctx.measureText(rg.label).width;
      const boxW = tw + 6;
      const isThis = ringIdx === i || hoverRing === i;
      const faded = (focusId != null || ringIdx != null) && !isThis;
      const emphasisAlpha = faded ? 0.33 : 1;
      const labelAlpha = fadeAlpha(fades.labels, String(i), emphasisAlpha, isThis) * (ringVis[i] ?? 1);
      if (labelAlpha < 0.01) {
        return;
      }
      ctx.globalAlpha = labelAlpha;
      ctx.fillStyle = rgba(bg, 1);
      ctx.fillRect(sx - boxW / 2, sy - 6, boxW, 13);
      ctx.fillStyle = shade(isThis ? 1 : 0.2);
      ctx.fillText(rg.label, sx, sy + 3);
      ctx.globalAlpha = 1;
      ringLabelRects.push({ h: 18, i, w: boxW + 6, x: sx - boxW / 2 - 3, y: sy - 10 });
    });
    const tipNode = focusId ? byId.get(focusId) : null;
    const tip = tipNode && seen(tipNode.rec) ? tipNode : null;
    let tipRect = null;
    if (tip) {
      const PADX = 6;
      const PADY = 4;
      const BADGE_H = 14;
      const ROW_GAP = 3;
      const LINE_H = 16;
      const ITEM_GAP = 8;
      const badgeFont = "9px ui-sans-serif, system-ui, sans-serif";
      const monoFont = "9px ui-monospace, SFMono-Regular, Menlo, monospace";
      const titleFont = "600 11px ui-sans-serif, system-ui, sans-serif";
      const footerFont = "9px ui-sans-serif, system-ui, sans-serif";
      const FOOTER_H = 13;
      const badgeFontFor = (i) => i === 0 ? badgeFont : monoFont;
      const badges = metaBadges(tip);
      const use = countLabel(tip);
      const titleText = tip.kind === "memory" ? memById.get(tip.id)?.body.split("\n")[0]?.trim() || tip.label : tip.label;
      const badgeW = badges.map((b, i) => {
        ctx.font = badgeFontFor(i);
        return ctx.measureText(b).width;
      });
      const rowW = badgeW.reduce((a, b) => a + b, 0) + ITEM_GAP * Math.max(0, badges.length - 1);
      ctx.font = monoFont;
      const useW = use ? ctx.measureText(use).width : 0;
      const metaW = rowW + (use ? ITEM_GAP + useW : 0);
      ctx.font = titleFont;
      const maxTitleW = Math.min(380, w - 16) - PADX * 2;
      const titleLines = wrapText(ctx, titleText, maxTitleW);
      const titleW = Math.max(0, ...titleLines.map((l) => ctx.measureText(l).width));
      const titleBgW = titleW + PADX * 2;
      const titleBgH = titleLines.length * LINE_H + PADY * 2;
      const footerText = nodeFooter(tip);
      ctx.font = footerFont;
      const footerW = footerText ? ctx.measureText(footerText).width : 0;
      const totalW = Math.max(metaW, footerW, titleBgW);
      const totalH = BADGE_H + ROW_GAP + titleBgH + (footerText ? ROW_GAP + FOOTER_H : 0);
      const bx = clamp(projX(tip.x) - totalW / 2, 4, Math.max(4, w - totalW - 4));
      const by = clamp(projY(tip.y) - (nodeRadius(tip) * nodeK + 8) - totalH, 4, Math.max(4, h - totalH - 4));
      tipRect = { h: totalH, w: totalW, x: bx, y: by };
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      const badgeMidY = by + BADGE_H / 2;
      ctx.fillStyle = shade(0.7);
      let cx = bx;
      badges.forEach((label, i) => {
        ctx.font = badgeFontFor(i);
        ctx.fillText(label, cx, badgeMidY);
        cx += badgeW[i] + ITEM_GAP;
      });
      if (use) {
        ctx.font = monoFont;
        ctx.fillStyle = shade(0.5);
        ctx.fillText(use, cx, badgeMidY);
      }
      const ty = by + BADGE_H + ROW_GAP;
      ctx.fillStyle = shade(1);
      ctx.fillRect(bx, ty, titleBgW, titleBgH);
      ctx.font = titleFont;
      ctx.fillStyle = inkInv;
      titleLines.forEach((line, i) => {
        ctx.fillText(line, bx + PADX, ty + PADY + LINE_H * i + LINE_H / 2);
      });
      if (footerText) {
        ctx.font = footerFont;
        ctx.fillStyle = shade(0.45);
        ctx.fillText(footerText, bx, ty + titleBgH + ROW_GAP + FOOTER_H / 2);
      }
      ctx.textBaseline = "alphabetic";
    }
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "center";
    const LBL_M = 6;
    const LBL_H = 15;
    const placed = ringLabelRects.map((r) => ({ h: r.h, w: r.w, x: r.x, y: r.y }));
    if (tipRect) {
      placed.push(tipRect);
    }
    for (const id of focusSet ?? []) {
      if (id === hoverId) {
        continue;
      }
      const n = byId.get(id);
      if (!n || !seen(n.rec)) {
        continue;
      }
      const label = ellipsize(ctx, n.label, Math.min(180, w * 0.32));
      const bw = ctx.measureText(label).width + 8;
      const x = clamp(projX(n.x) - bw / 2, LBL_M, Math.max(LBL_M, w - bw - LBL_M));
      const top = projY(n.y) - (nodeRadius(n) * nodeK + 7) - LBL_H + 4;
      const clampY = (v) => clamp(v, LBL_M, Math.max(LBL_M, h - LBL_H - LBL_M));
      const step = LBL_H + 3;
      let y = null;
      for (let k = 0; k <= 7 && y == null; k += 1) {
        for (const dy of k === 0 ? [0] : [-k * step, k * step]) {
          const cand = { h: LBL_H, w: bw, x, y: clampY(top + dy) };
          if (!placed.some((p) => rectsOverlap(cand, p))) {
            y = cand.y;
            break;
          }
        }
      }
      if (y == null) {
        continue;
      }
      placed.push({ h: LBL_H, w: bw, x, y });
      ctx.fillStyle = chipBg;
      ctx.fillRect(x, y, bw, LBL_H);
      ctx.fillStyle = shade(0.85);
      ctx.fillText(label, x + bw / 2, y + 11);
    }
    return { animating, ringLabelRects };
  }
  var SCRAMBLE_RADIUS = 6;
  var SCRAMBLE_CELL_MIN = 5;
  var SCRAMBLE_CELL_MAX = 13;
  function drawScramble({
    ctx,
    dpr,
    palette,
    rings,
    vp
  }) {
    const { bg, darkTheme, primary } = palette;
    const projX = (wx) => wx * vp.k + vp.x;
    const projY = (wy) => wy * vp.k * TILT + vp.y;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const coreX = projX(0);
    const coreY = projY(0);
    const coreRx = (rings[0]?.r ?? RING_INNER) * vp.k * 1.25;
    if (coreRx <= 0) {
      return;
    }
    const washR = coreRx * 1.15;
    ctx.save();
    ctx.translate(coreX, coreY);
    ctx.scale(1, TILT);
    const wash = ctx.createRadialGradient(0, 0, 0, 0, 0, washR);
    wash.addColorStop(0, rgba(bg, darkTheme ? 0.9 : 0.93));
    wash.addColorStop(0.62, rgba(bg, darkTheme ? 0.84 : 0.88));
    wash.addColorStop(1, rgba(bg, 0));
    ctx.fillStyle = wash;
    ctx.beginPath();
    ctx.arc(0, 0, washR, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
    const cell = clamp(coreRx / SCRAMBLE_RADIUS, SCRAMBLE_CELL_MIN, SCRAMBLE_CELL_MAX);
    const coreRy = coreRx * TILT;
    const half = Math.max(3, Math.round(coreRx / cell));
    const now = performance.now();
    const t = now / 1e3;
    ctx.save();
    ctx.font = `${cell}px "JetBrains Mono", "Hiragino Sans", "Noto Sans JP", ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let r = -half; r <= half; r += 1) {
      const rowSeed = r * 19349663 >>> 0 || 1;
      const dir = rowSeed & 1 ? 1 : -1;
      const speed = 8 + rowSeed % 16;
      const scroll = now / 1e3 * speed * dir;
      const ny = r * cell / coreRy;
      const rowDim = 1 - 0.5 * Math.min(1, Math.abs(ny));
      const kMin = Math.floor((-coreRx - scroll) / cell) - 1;
      const kMax = Math.ceil((coreRx - scroll) / cell) + 1;
      for (let k = kMin; k <= kMax; k += 1) {
        const sx = k * cell + scroll;
        const nx = sx / coreRx;
        const d2 = nx * nx + ny * ny;
        if (d2 > 1) {
          continue;
        }
        const seed = (rowSeed ^ (k >>> 0) * 73856093) >>> 0;
        const ch = SCRAMBLE_CHARS[seed % SCRAMBLE_CHARS.length] ?? "0";
        const edge = clamp((1 - Math.sqrt(d2)) / 0.4, 0, 1);
        const flick = 0.7 + 0.3 * ((seed >>> 5) % 100 / 100);
        const phase = (seed & 7) * 0.35;
        const glow = Math.sin(nx * 4.5 + t * 1.3 + phase) * Math.sin(ny * 4.5 - t * 0.9 + phase);
        const pop = 1 + clamp((glow - 0.25) / 0.75, 0, 1) * 2.6;
        const a = clamp((darkTheme ? 0.22 : 0.3) * edge * flick * rowDim * pop, 0, 0.9);
        if (a < 0.02) {
          continue;
        }
        ctx.fillStyle = rgba(primary, a);
        ctx.fillText(ch, coreX + sx, coreY + r * cell);
      }
    }
    ctx.restore();
    ctx.globalAlpha = 1;
  }
  return __toCommonJS(entry_exports);
})();

// ─────────────── end of the inlined renderer ───────────────

// ── Page ────────────────────────────────────────────────────────────────────
// The renderer comes from the Hermes starmap (render.ts, color.ts, geometry.ts,
// text.ts), compiled as-is and inlined above as `Starmap`. Below: the scene
// contract, the camera and the interactions.


const selectedId$ = atom(null)
const hoverId$ = atom(null)
const zoom$ = atom(1)
const refreshTick$ = atom(0)
const detailCache$ = atom(null)
const CLE_INFLUENCE = 'carnet.graph.influence'
const influence$ = atom((() => {
  try {
    const v = parseFloat(localStorage.getItem(CLE_INFLUENCE))
    return Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0.5
  } catch (_) { return 0.5 }
})())
const reglages$ = atom(false)
const erreurRendu$ = atom('')

const TILT = Starmap.TILT
const ZOOM_MIN = Starmap.ZOOM_MIN
const ZOOM_MAX = Starmap.ZOOM_MAX

// Per-canvas state (pan/zoom), outside React.
const _state = {}

function etatCanvas(nid) {
  if (!_state[nid]) {
    _state[nid] = { canvas: null, vp: null, drag: null, size: { w: 0, h: 0 } }
  }
  return _state[nid]
}

/** Builds the scene drawScene expects, from the backend JSON. */
function construireScene(graphe, canvas, st, palette, hover, selected) {
  const rect = canvas.getBoundingClientRect()
  const w = Math.max(1, rect.width)
  const h = Math.max(1, rect.height)
  const outer = graphe.rings.length ? graphe.rings[graphe.rings.length - 1].r : Starmap.RING_OUTER

  if (!st.vp || st.vp._init !== `${w}x${h}`) {
    st.vp = Starmap.fitViewport(w, h, outer)
    st.vp._init = `${w}x${h}`
  }

  const nodes = graphe.nodes.map((n) => {
    const rec = n.rec ?? 0
    return {
      ...n,
      // 'memory' draws a diamond, 'skill' a circle: that is the starmap convention.
      kind: n.kind === 'fiche' ? 'memory' : 'skill',
      label: n.title,
      rec,
      tr: Starmap.radiusForRecency(rec, outer),
      outerRingIndex: n.ring ?? 0,
      useCount: 0,
      state: 'active',
      pinned: false
    }
  })

  const byId = new Map(nodes.map((n) => [n.id, n]))
  const links = graphe.edges.map((e) => ({ source: e.source, target: e.target, weight: e.weight }))

  const adjacency = new Map()
  for (const e of links) {
    if (!adjacency.has(e.source)) adjacency.set(e.source, new Set())
    if (!adjacency.has(e.target)) adjacency.set(e.target, new Set())
    adjacency.get(e.source).add(e.target)
    adjacency.get(e.target).add(e.source)
  }

  // No entrance animation: everything is already born.
  const fades = {
    appear: new Map(), labels: new Map(), links: new Map(), nodes: new Map(), rings: new Map()
  }

  return {
    adjacency,
    byId,
    ctx: canvas.getContext('2d'),
    dpr: Math.min(2, window.devicePixelRatio || 1),
    fades,
    focusId: selected,
    hoverId: hover,
    hoverLink: null,
    hoverRing: null,
    links,
    memById: new Map(),
    nodes,
    palette,
    reveal: 1,
    rings: graphe.rings.map((r, i) => ({ r: r.r, label: r.label, ratio: (i + 1) / graphe.rings.length })),
    selectedRing: null,
    size: { h, w },
    snapMotion: true,
    vp: st.vp
  }
}

/**
 * Two layers: the scene is painted once into an offscreen canvas and each frame
 * only copies it and animates the scramble. At 1,400 nodes, repainting 3,400
 * links every frame cost more than everything else combined.
 */
function peindre(st, graphe, force) {
  const canvas = st.canvas
  if (!canvas || !graphe) return
  const rect = canvas.getBoundingClientRect()
  const w = Math.max(1, rect.width)
  const h = Math.max(1, rect.height)
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr)
    canvas.height = Math.round(h * dpr)
    force = true
  }
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  if (!st.palette || st._paletteTaille !== `${w}x${h}`) {
    st.palette = Starmap.computePalette(canvas)
    st._paletteTaille = `${w}x${h}`
  }
  const palette = st.palette

  const signature = [
    st.vp && `${st.vp.k.toFixed(3)}:${Math.round(st.vp.x)}:${Math.round(st.vp.y)}`,
    hoverId$.get() || '', selectedId$.get() || '', canvas.width, canvas.height
  ].join('|')

  try {
    if (force || !st.off || st._sig !== signature) {
      if (!st.off) st.off = document.createElement('canvas')
      if (st.off.width !== canvas.width || st.off.height !== canvas.height) {
        st.off.width = canvas.width
        st.off.height = canvas.height
      }
      const octx = st.off.getContext('2d')
      const scene = construireScene(graphe, canvas, st, palette, hoverId$.get(), selectedId$.get())
      scene.ctx = octx
      octx.setTransform(1, 0, 0, 1, 0, 0)
      octx.clearRect(0, 0, st.off.width, st.off.height)
      Starmap.drawScene(scene)
      st._sig = signature
      st._scene = scene
    }

    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(st.off, 0, 0)
    if (st._scene) {
      // The scramble effect scales with the radius of the FIRST ring, so it becomes
      // huge when the data rings start far from the centre. It gets its own fixed
      // size instead, independent of the map.
      Starmap.drawScramble({
        ctx, dpr: st._scene.dpr, palette,
        rings: [{ r: 114 }, ...(st._scene.rings || [])],
        vp: st._scene.vp
      })
    }
    if (erreurRendu$.get()) erreurRendu$.set('')
  } catch (err) {
    erreurRendu$.set(`${(err && err.message) || err}`)
    console.error('[carnet-graphe] drawScene', err)
  }
}

/** The node under the cursor, in the renderer's screen space. */
function sousCurseur(st, graphe, e) {
  const canvas = st.canvas
  if (!canvas || !st.vp) return null
  const rect = canvas.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const my = e.clientY - rect.top
  const vp = st.vp
  let best = null
  let bestD = Infinity
  for (const n of graphe.nodes) {
    // Same projection as the renderer's projX/projY, with vp.y offset by 5%.
    const sx = n.x * vp.k + vp.x
    const sy = n.y * vp.k * TILT + vp.y
    const r = (n.kind === 'fiche' ? 4.4 : 3.4) * st.vp.k * 2.2 + 6
    const d = (sx - mx) ** 2 + (sy - my) ** 2
    if (d < r * r && d < bestD) { bestD = d; best = n.id }
  }
  return best
}

function centrerSur(id, graphe) {
  const st = etatCanvas('principal')
  const n = graphe && graphe.nodes.find((x) => x.id === id)
  if (!n || !st.canvas) return
  const rect = st.canvas.getBoundingClientRect()
  const k = (st.vp && st.vp.k) || 1
  st.vp = { ...(st.vp || {}), k, x: rect.width / 2 - n.x * k, y: rect.height / 2 - n.y * k }
}

/** Memories attached to `id`: semantic links first, then the same topic. */
function lierSouvenirs(graphe, id) {
  if (!graphe || !id) return []
  const parId = new Map(graphe.nodes.map((n) => [n.id, n]))
  const scores = new Map()
  for (const e of graphe.edges) {
    if (e.source === id) scores.set(e.target, e.weight)
    else if (e.target === id) scores.set(e.source, e.weight)
  }
  const moi = parId.get(id)
  if (moi && moi.cluster) {
    for (const n of graphe.nodes) {
      if (n.id !== id && !scores.has(n.id) && n.cluster === moi.cluster) scores.set(n.id, 0.001)
    }
  }
  return [...scores.entries()]
    .filter(([nid]) => parId.has(nid))
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10)
    .map(([nid, poids]) => ({ node: parId.get(nid), poids }))
}

async function ouvrirDetail(id) {
  if (!_ctx || !id) return
  selectedId$.set(id)
  try {
    detailCache$.set(await api(`/node/${encodeURIComponent(id)}`))
  } catch (e) {
    detailCache$.set({ id, title: id, text: '', error: String((e && e.message) || e) })
  }
}

function GrapheCanvas({ graphe }) {
  const st = etatCanvas('principal')
  const refGraphe = useRef(graphe)
  refGraphe.current = graphe

  // The starmap is an animated scene (the scramble breathes continuously), so one
  // frame per interaction is not enough: it needs a loop.
  useEffect(() => {
    let vivant = true
    let raf = 0
    let dernier = 0
    const frame = (ts) => {
      if (!vivant) return
      if (ts - dernier >= 33) {
        dernier = ts
        peindre(st, refGraphe.current, false)
      }
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => { vivant = false; cancelAnimationFrame(raf) }
  }, [])

  return jsx('canvas', {
    ref: (el) => { st.canvas = el },
    className: 'block h-full w-full cursor-grab touch-none select-none',
    onMouseDown: (e) => {
      if (e.button !== 0) return
      st.drag = { id: sousCurseur(st, graphe, e), moved: false, sx: e.clientX, sy: e.clientY, vp: { ...st.vp } }
    },
    onMouseMove: (e) => {
      const d = st.drag
      if (d && d.id === null) {
        const dx = e.clientX - d.sx
        const dy = e.clientY - d.sy
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) d.moved = true
        if (d.moved) {
          st.vp = { ...d.vp, x: d.vp.x + dx, y: d.vp.y + dy, _init: st.vp._init }
        }
        return
      }
      const id = sousCurseur(st, graphe, e)
      if (id !== hoverId$.get()) hoverId$.set(id)
    },
    onMouseUp: () => {
      const d = st.drag
      st.drag = null
      if (d && d.id && !d.moved) void ouvrirDetail(d.id)
      else if (d && !d.id && !d.moved) { selectedId$.set(null); detailCache$.set(null) }
    },
    onMouseLeave: () => { hoverId$.set(null); st.drag = null },
    onDoubleClick: () => {
      const rect = st.canvas.getBoundingClientRect()
      const outer = graphe.rings.length ? graphe.rings[graphe.rings.length - 1].r : Starmap.RING_OUTER
      st.vp = Starmap.fitViewport(rect.width, rect.height, outer)
      st.vp._init = `${Math.round(rect.width)}x${Math.round(rect.height)}`
    },
    onWheel: (e) => {
      e.preventDefault()
      const rect = st.canvas.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      const vp = st.vp
      const k = Starmap.clamp(vp.k * (e.deltaY > 0 ? 0.9 : 1.1), ZOOM_MIN, ZOOM_MAX)
      st.vp = { k, x: px - ((px - vp.x) / vp.k) * k, y: py - ((py - vp.y) / vp.k) * k, _init: vp._init }
    }
  })
}

function PanneauDetail({ ouvert, graphe }) {
  const detail = useValue(detailCache$)
  // Fixed width: without min/max a long word widens the column and the layout jumps.
  // Opening is a width + opacity transition, not a hard mount.
  const style = {
    width: ouvert ? 360 : 0,
    minWidth: ouvert ? 360 : 0,
    maxWidth: ouvert ? 360 : 0,
    flex: `0 0 ${ouvert ? 360 : 0}px`,
    opacity: ouvert ? 1 : 0,
    transform: ouvert ? 'translateX(0)' : 'translateX(12px)',
    transition: 'width 180ms cubic-bezier(0.22,1,0.36,1), flex-basis 180ms cubic-bezier(0.22,1,0.36,1), opacity 160ms ease, transform 180ms cubic-bezier(0.22,1,0.36,1)',
    overflow: 'hidden'
  }

  if (!detail) {
    return jsx('div', { style, className: 'h-full' })
  }

  const date = detail.timestamp ? new Date(detail.timestamp * 1000).toLocaleString() : ''
  const lies = lierSouvenirs(graphe, detail.id)

  return jsxs('div', {
    style,
    className: 'flex h-full flex-col gap-2 overflow-y-auto border-l border-(--border)/60 pl-3 text-xs',
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-start justify-between gap-2',
        children: [
          jsx('div', { className: 'font-medium text-(--ui-text-secondary)', children: detail.title || detail.id }),
          jsx('button', {
            type: 'button', className: 'text-(--ui-text-tertiary)',
            onClick: () => { selectedId$.set(null); detailCache$.set(null); refreshTick$.set(refreshTick$.get() + 1) },
            children: '✕'
          })
        ]
      }),
      date ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: date }) : null,
      detail.error ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: detail.error }) : null,
      jsx('div', {
        style: { flex: '0 0 46%', minHeight: 0 },
        className: 'overflow-y-auto whitespace-pre-wrap rounded border border-(--border)/40 px-2 py-1.5 text-[0.75rem] leading-snug text-(--ui-text-tertiary)',
        children: detail.text || '—'
      }),
      detail.keywords && detail.keywords.length
        ? jsxs('div', {
            className: 'flex flex-wrap gap-1',
            children: detail.keywords.map((k) => jsx(Badge, { variant: 'muted', children: k }, k))
          })
        : null,
      lies.length
        ? jsxs('div', {
            className: 'flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto border-t border-(--border)/40 pt-2',
            children: [
              jsx('div', {
                className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)',
                children: t('dashboard.graph.linked', { count: lies.length })
              }),
              ...lies.map(({ node, poids }) => jsx('button', {
                key: node.id,
                type: 'button',
                title: node.text || node.title || node.id,
                className: 'carnet-link-row',
                onClick: () => { ouvrirDetail(node.id); centrerSur(node.id, graphe) },
                children: `${poids >= 0.01 ? '↳' : '·'} ${node.title || node.id}${node.text ? ' — ' + node.text : ''}`
              }, node.id))
            ]
          })
        : null,
      jsxs('div', {
        className: 'flex shrink-0 flex-wrap gap-1.5 pt-1',
        children: [
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => centrerSur(detail.id, graphe), children: t('dashboard.graph.recenter') }),
          jsx(Button, {
            size: 'sm', variant: 'ghost',
            onClick: () => { try { navigator.clipboard.writeText(detail.text || '') } catch (_) {} },
            children: t('dashboard.graph.copy')
          })
        ]
      })
    ]
  })
}

function GraphsView() {
  const tick = useValue(refreshTick$)
  const selected = useValue(selectedId$)
  const erreurRendu = useValue(erreurRendu$)
  const influence = useValue(influence$)
  const reglages = useValue(reglages$)
  const { data, isLoading, error } = useQuery({
    queryKey: ['carnet-graphe', 'graph', tick, influence],
    queryFn: () => api(`/graph?influence=${influence}`),
    staleTime: 0
  })

  if (error) return jsx(ErrorState, { title: t('errors.backend_unreachable'), message: String(error.message || error) })
  if (isLoading && !data) return jsx(Skeleton, { className: 'h-full w-full' })
  if (!data || !data.stats || data.stats.nodes === 0) {
    return jsx(EmptyState, { title: t('dashboard.graph.empty_title'), message: t('dashboard.graph.empty_message') })
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-2 p-3',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-3',
        children: [
          jsxs('div', {
            className: 'flex flex-col',
            children: [
              jsx('span', { className: 'text-sm font-medium text-(--ui-text-primary)', children: t('dashboard.graph.title') }),
              jsx('span', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: t('dashboard.graph.stats', { nodes: data.stats.nodes, facts: data.stats.facts, sheets: data.stats.fiches, edges: data.stats.edges })
              })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-1.5',
            children: [
              jsx(Button, {
                size: 'sm', variant: reglages ? 'outline' : 'ghost',
                onClick: () => reglages$.set(!reglages$.get()),
                children: reglages ? t('dashboard.graph.settings_on') : t('dashboard.graph.settings')
              }),
              jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => refreshTick$.set(refreshTick$.get() + 1), children: t('dashboard.page.refresh') })
            ]
          })
        ]
      }),
      reglages
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-3 rounded-md border border-(--border)/50 px-2.5 py-2',
            children: [
              jsxs('div', { className: 'flex items-center gap-2', children: [
                jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: t('dashboard.graph.influence') }),
                jsx('input', {
                  type: 'range', min: 0, max: 1, step: 0.1, value: influence,
                  className: 'h-1 w-44 cursor-pointer accent-(--accent)',
                  onChange: (e) => {
                    const v = Math.round(parseFloat(e.target.value) * 10) / 10
                    influence$.set(v)
                    try { localStorage.setItem(CLE_INFLUENCE, String(v)) } catch (_) {}
                  }
                }),
                jsx('span', {
                  className: 'w-9 text-right text-[0.6875rem] tabular-nums text-(--ui-text-secondary)',
                  children: `${Math.round(influence * 100)} %`
                })
              ] }),
              jsx('span', {
                className: 'text-[0.625rem] text-(--ui-text-quaternary)',
                children: t('dashboard.graph.influence_note')
              })
            ]
          })
        : null,
      jsx('div', {
        className: erreurRendu ? 'rounded border border-(--ui-danger)/40 px-2 py-1 font-mono text-[0.625rem] text-(--ui-text-tertiary)' : 'hidden',
        children: erreurRendu
      }),
      jsxs('div', {
        className: 'flex min-h-0 flex-1',
        children: [
          jsx('div', {
            className: 'relative min-w-0 flex-1 rounded-md border border-(--border)/50',
            children: jsx(GrapheCanvas, { graphe: data })
          }),
          jsx(PanneauDetail, { ouvert: !!selected, graphe: data })
        ]
      })
    ]
  })
}


// ── Page ────────────────────────────────────────────────────────────────────
// ── Onboarding ──────────────────────────────────────────────────────────────
// One question per screen. A single stacked form would be shorter to write and
// much worse to use: the user cannot tell what matters. Seven short steps, a
// progress bar, Back and Skip everywhere, and nothing here is final — every
// answer stays editable in Settings.
const ONBOARD_STEPS = ['welcome', 'language', 'embedding', 'model', 'memory', 'notes', 'finish']

// The wizard's progress lives outside the component, and outside the JS module
// as well: changing the interface language replays the plugin bundle, so both a
// useState and a plain module variable send the user back to the first screen.
// sessionStorage is the only thing that survives that reload.
const OB_DRAFT_DEFAULT = { step: 0, language: '', embedding: 'local', embedUrl: '', embedModel: '', model: 'off', notes: true }
const OB_DRAFT_KEY = 'carnet-onboarding-draft'

function obLoadDraft() {
  try {
    const raw = sessionStorage.getItem(OB_DRAFT_KEY)
    const parsed = raw ? JSON.parse(raw) : null
    if (parsed && typeof parsed === 'object') return { ...OB_DRAFT_DEFAULT, ...parsed }
  } catch (_) {}
  return { ...OB_DRAFT_DEFAULT }
}

function obSaveDraft(draft) {
  try { sessionStorage.setItem(OB_DRAFT_KEY, JSON.stringify(draft)) } catch (_) {}
}

function obClearDraft() {
  try { sessionStorage.removeItem(OB_DRAFT_KEY) } catch (_) {}
}

const obDraft$ = atom(obLoadDraft())

function ObChoice({ active, title, hint, warn, onClick }) {
  return jsxs('button', {
    type: 'button',
    onClick,
    className: 'carnet-ob-choice' + (active ? ' is-active' : '') + (warn ? ' is-warn' : ''),
    children: [
      jsxs('div', {
        className: 'carnet-ob-choice-head',
        children: [
          jsx('span', { className: 'carnet-ob-choice-title', children: title }),
          warn ? jsx('span', { className: 'carnet-ob-warn', children: t('onboarding.embedding.warning') }) : null,
          jsx('span', { className: 'carnet-ob-dot' + (active ? ' is-on' : '') })
        ]
      }),
      hint ? jsx('span', { className: 'carnet-ob-choice-hint', children: hint }) : null
    ]
  })
}

function Onboarding() {
  const tick = useValue(tick$)
  const { data: settings } = useQuery({ queryKey: ['carnet', 'settings', tick], queryFn: () => api('/settings'), staleTime: 0 })
  const { data: emb } = useQuery({ queryKey: ['carnet', 'embedding', tick], queryFn: () => api('/settings/embedding'), staleTime: 0 })
  const { data: mem } = useQuery({ queryKey: ['carnet', 'memory-settings', tick], queryFn: () => api('/settings/memory'), staleTime: 0 })
  const { data: modelCfg } = useQuery({ queryKey: ['carnet', 'model-settings', tick], queryFn: () => api('/settings/model'), staleTime: 0 })
  const draft = useValue(obDraft$)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const { step, language, embedding, model, notes } = draft
  const patch = (values) => {
    const next = { ...draft, ...values }
    obSaveDraft(next)
    obDraft$.set(next)
    return next
  }

  const chooseLanguage = async (code) => {
    patch({ language: code })
    try {
      await api('/settings/language', { method: 'POST', body: { language: code } })
      await refresh()
    } catch (_) {}
  }
  const setStep = (n) => patch({ step: n })

  useEffect(() => {
    if (settings && !draft.language) patch({ language: settings.language || 'en' })
  }, [settings, draft.language])

  const local = (emb && emb.local) || { state: 'absent', percent: 0 }
  // Turning the rephrase model on means pointing it at a real model name. If the
  // user never configured one, the honest answer is "not yet", not a guess.
  const modelReady = !!(modelCfg && modelCfg.mode !== 'off' && modelCfg.model)
  useEffect(() => {
    if (local.state !== 'downloading') return undefined
    const id = setInterval(() => { void refresh() }, 1500)
    return () => clearInterval(id)
  }, [local.state])

  const current = ONBOARD_STEPS[step]
  const total = ONBOARD_STEPS.length - 1
  const pct = Math.round(100 * step / total)

  async function commit(next) {
    setError('')
    setBusy(true)
    try {
      if (current === 'embedding') {
        if (embedding === 'custom' && (!draft.embedUrl.trim() || !draft.embedModel.trim())) {
          setError(t('onboarding.embedding.need_fields'))
          setBusy(false)
          return
        }
        const body = embedding === 'custom'
          ? { mode: 'custom', url: draft.embedUrl.trim(), model: draft.embedModel.trim() }
          : { mode: embedding }
        await api('/settings/embedding', { method: 'POST', body })
      } else if (current === 'model') {
        if (model === 'off') await api('/settings/model', { method: 'POST', body: { mode: 'off' } })
      } else if (current === 'notes') {
        await api('/settings/notes', { method: 'POST', body: { enabled: notes } })
      }
    } catch (e) {
      setError(String((e && e.message) || e))
      setBusy(false)
      return
    }
    setBusy(false)
    setStep(next)
    if (current === 'embedding') await refresh()
  }

  const close = async () => {
    // The language is applied last on purpose: switching it replays the plugin
    // bundle, and doing that mid-flow restarts the wizard from screen one.
    const chosen = obLoadDraft().language
    if (chosen && settings && chosen !== settings.language) {
      try {
        await api('/settings/language', { method: 'POST', body: { language: chosen } })
      } catch (_) {}
    }
    obClearDraft()
    obDraft$.set({ ...OB_DRAFT_DEFAULT })
    try {
      await api('/settings/onboarding', { method: 'POST', body: { done: true } })
    } catch (e) {
      notifyError(e, t('errors.settings_failed'))
    }
    await refresh()
  }

  const pickModel = async () => {
    if (modelReady) { patch({ model: 'on' }); return }
    setError('')
    setBusy(true)
    try {
      await api('/settings/model/auto', { method: 'POST', body: {} })
      patch({ model: 'on' })
      await refresh()
    } catch (e) {
      setError(t('onboarding.model.no_server'))
    }
    setBusy(false)
  }

  const downloadLocal = async () => {
    setError('')
    setBusy(true)
    try {
      await api('/settings/embedding/download', { method: 'POST', body: { consent: true }, timeoutMs: 30000 })
      await refresh()
    } catch (e) {
      setError(String((e && e.message) || e))
    }
    setBusy(false)
  }

  const embedOptions = [
    ['local', t('onboarding.embedding.local'), t('onboarding.embedding.local_hint')],
    ['lmstudio', t('onboarding.embedding.lmstudio'), t('onboarding.embedding.lmstudio_hint')],
    ['custom', t('onboarding.embedding.custom'), t('onboarding.embedding.custom_hint')],
    ['disabled', t('onboarding.embedding.disabled'), t('onboarding.embedding.disabled_hint')]
  ]

  const languages = (settings && settings.languages) || ['en', 'fr']

  let body = null
  if (current === 'welcome') {
    body = jsxs('div', {
      className: 'carnet-ob-center',
      children: [
        jsx('h1', { className: 'carnet-ob-title', children: t('onboarding.welcome.title') }),
        jsx('p', { className: 'carnet-ob-lead', children: t('onboarding.welcome.body') }),
        jsxs('div', {
          className: 'carnet-ob-layers',
          children: [
            jsx('div', { className: 'carnet-ob-layer', children: t('onboarding.welcome.layer_core') }),
            jsx('div', { className: 'carnet-ob-layer', children: t('onboarding.welcome.layer_facts') }),
            jsx('div', { className: 'carnet-ob-layer', children: t('onboarding.welcome.layer_sheets') })
          ]
        }),
        jsx('p', { className: 'carnet-ob-promise', children: t('onboarding.welcome.promise') }),
        jsx(Button, { className: 'carnet-ob-cta', onClick: () => commit(1), children: t('onboarding.continue') }),
        jsx('button', {
          type: 'button',
          className: 'carnet-ob-skip',
          onClick: close,
          children: t('onboarding.welcome.skip_hint')
        })
      ]
    })
  } else if (current === 'language') {
    body = jsxs('div', {
      className: 'carnet-ob-stack',
      children: [
        jsx('h2', { className: 'carnet-ob-title', children: t('onboarding.language.title') }),
        jsx('p', { className: 'carnet-ob-lead', children: t('onboarding.language.body', { count: languages.length }) }),
        jsx('div', {
          className: 'carnet-ob-grid',
          children: languages.map((code) => jsx(ObChoice, {
            active: language === code,
            title: String(code).toUpperCase(),
            hint: null,
            onClick: () => { void chooseLanguage(code) }
          }, code))
        })
      ]
    })
  } else if (current === 'embedding') {
    body = jsxs('div', {
      className: 'carnet-ob-stack',
      children: [
        jsx('h2', { className: 'carnet-ob-title', children: t('onboarding.embedding.title') }),
        jsx('p', { className: 'carnet-ob-lead', children: t('onboarding.embedding.body') }),
        jsx('div', {
          className: 'carnet-ob-stack-tight',
          children: embedOptions.map(([key, title, hint]) => jsx(ObChoice, {
            active: embedding === key,
            title,
            hint,
            warn: key === 'disabled',
            onClick: () => patch({ embedding: key })
          }, key))
        }),
        embedding === 'custom' ? jsxs('div', {
          className: 'carnet-ob-fields',
          children: [
            jsx(Field, {
              label: t('onboarding.embedding.url'), value: draft.embedUrl,
              onChange: (url) => patch({ embedUrl: url }), placeholder: 'http://127.0.0.1:1234/v1'
            }),
            jsx(Field, {
              label: t('onboarding.embedding.model'), value: draft.embedModel,
              onChange: (model) => patch({ embedModel: model }), placeholder: 'text-embedding-embeddinggemma-300m'
            })
          ]
        }) : null,
        embedding === 'local' ? jsxs('div', {
          className: 'carnet-ob-download',
          children: [
            local.state === 'ready'
              ? jsx(Badge, { children: t('onboarding.embedding.ready') })
              : jsx(Button, {
                  size: 'sm',
                  variant: 'outline',
                  disabled: busy || local.state === 'downloading',
                  onClick: downloadLocal,
                  children: local.state === 'downloading'
                    ? t('onboarding.embedding.downloading', { percent: Math.round(local.percent || 0) })
                    : t('onboarding.embedding.download')
                }),
            local.state === 'downloading' || (local.state === 'ready' ? false : true)
              ? jsx('div', {
                  className: 'carnet-ob-bar',
                  children: jsx('i', { style: { width: Math.max(2, Math.round(local.percent || 0)) + '%' } })
                })
              : null
          ]
        }) : null
      ]
    })
  } else if (current === 'model') {
    body = jsxs('div', {
      className: 'carnet-ob-stack',
      children: [
        jsx('h2', { className: 'carnet-ob-title', children: t('onboarding.model.title') }),
        jsx('p', { className: 'carnet-ob-lead', children: t('onboarding.model.body') }),
        jsx('div', {
          className: 'carnet-ob-grid',
          children: [
            jsx(ObChoice, { active: model === 'off', title: t('onboarding.model.off_label'), hint: t('onboarding.model.off_hint'), onClick: () => patch({ model: 'off' }) }, 'off'),
            jsx(ObChoice, {
              active: model === 'on',
              title: t('onboarding.model.on_label'),
              hint: modelReady ? t('onboarding.model.on_hint') : t('onboarding.model.auto_hint'),
              onClick: pickModel
            }, 'on')
          ]
        })
      ]
    })
  } else if (current === 'memory') {
    const fields = (mem && mem.fields) || []
    const pick = (key) => {
      const f = fields.filter((x) => x.key === key)[0]
      return f ? f.value : null
    }
    body = jsxs('div', {
      className: 'carnet-ob-stack',
      children: [
        jsx('h2', { className: 'carnet-ob-title', children: t('onboarding.memory.title') }),
        jsx('p', { className: 'carnet-ob-lead', children: t('onboarding.memory.body') }),
        jsxs('div', {
          className: 'carnet-ob-summary',
          children: [
            jsxs('div', { className: 'carnet-ob-sum-row', children: [
              jsx('span', { children: t('onboarding.memory.seats') }),
              jsx('strong', { children: String(pick('seat_limit') == null ? '25' : pick('seat_limit')) })
            ]}),
            jsxs('div', { className: 'carnet-ob-sum-row', children: [
              jsx('span', { children: t('onboarding.memory.facts_per_turn') }),
              jsx('strong', { children: String(pick('max_facts_per_turn') == null ? '14' : pick('max_facts_per_turn')) })
            ]}),
            jsxs('div', { className: 'carnet-ob-sum-row', children: [
              jsx('span', { children: t('onboarding.memory.budget') }),
              jsx('strong', { children: String(pick('budget_chars_turn') == null ? '2400' : pick('budget_chars_turn')) })
            ]})
          ]
        }),
        jsx('p', { className: 'carnet-ob-note', children: t('onboarding.memory.hint') })
      ]
    })
  } else if (current === 'notes') {
    body = jsxs('div', {
      className: 'carnet-ob-stack',
      children: [
        jsx('h2', { className: 'carnet-ob-title', children: t('onboarding.notes.title') }),
        jsx('p', { className: 'carnet-ob-lead', children: t('onboarding.notes.body') }),
        jsx('div', {
          className: 'carnet-ob-grid',
          children: [
            jsx(ObChoice, { active: notes, title: t('onboarding.notes.on_label'), hint: t('onboarding.notes.on_hint'), onClick: () => patch({ notes: true }) }, 'on'),
            jsx(ObChoice, { active: !notes, title: t('onboarding.notes.off_label'), hint: t('onboarding.notes.off_hint'), onClick: () => patch({ notes: false }) }, 'off')
          ]
        })
      ]
    })
  } else {
    body = jsxs('div', {
      className: 'carnet-ob-center',
      children: [
        jsx('h1', { className: 'carnet-ob-title', children: t('onboarding.finish.title') }),
        jsx('p', { className: 'carnet-ob-lead', children: t('onboarding.finish.body') }),
        jsx(Button, { className: 'carnet-ob-cta', disabled: busy, onClick: close, children: t('onboarding.finish.go') })
      ]
    })
  }

  return jsxs('div', {
    className: 'carnet-ob',
    children: [
      jsx('div', { className: 'carnet-ob-content', children: body }),
      error ? jsx('div', { className: 'carnet-ob-error', children: error }) : null,
      current === 'welcome' ? null : jsxs('div', {
        className: 'carnet-ob-foot',
        children: [
          jsx('div', {
            className: 'carnet-ob-progress',
            children: jsx('i', { style: { width: Math.max(4, pct) + '%' } })
          }),
          jsxs('div', {
            className: 'carnet-ob-foot-row',
            children: [
              jsx(Button, { size: 'sm', variant: 'ghost', disabled: busy || step === 0, onClick: () => setStep(Math.max(0, step - 1)), children: t('onboarding.back') }),
              jsx('span', { className: 'carnet-ob-count', children: t('onboarding.progress', { current: step, total }) }),
              current === 'finish'
                ? jsx(Button, { size: 'sm', disabled: busy, onClick: close, children: t('onboarding.finish.go') })
                : jsx(Button, { size: 'sm', disabled: busy, onClick: () => commit(step + 1), children: t('onboarding.continue') }),
              jsx('button', { type: 'button', className: 'carnet-ob-skip', onClick: close, children: t('onboarding.skip') })
            ]
          })
        ]
      })
    ]
  })
}

function BackendMissing() {
  return jsxs('div', {
    className: 'carnet-ob',
    children: jsx('div', {
      className: 'carnet-ob-content',
      children: jsxs('div', {
        className: 'carnet-ob-center',
        children: [
          jsx('h1', { className: 'carnet-ob-title', children: 'Carnet is not enabled in this profile' }),
          jsx('p', { className: 'carnet-ob-lead', children: 'The interface is installed for the whole app, but the memory itself lives inside a profile. This one does not have it yet, so every request comes back empty.' }),
          jsx('pre', { className: 'carnet-ob-cmd', children: 'hermes -p <profile> plugins install 404BugX/carnet' }),
          jsx('p', { className: 'carnet-ob-lead', children: 'Run it in a terminal with the profile name, then retry. Without a profile name it targets the default one.' }),
          jsx(Button, {
            className: 'carnet-ob-cta',
            onClick: async () => { await probeBackend(); await refresh() },
            children: 'Retry'
          })
        ]
      })
    })
  })
}

function CarnetPage() {
  const tab = useValue(tab$)
  const tick = useValue(tick$)
  const { data: settings } = useQuery({
    queryKey: ['carnet', 'settings', tick], queryFn: () => api('/settings'), staleTime: 0
  })
  const missing = useValue(backendMissing$)
  if (missing) return jsx(BackendMissing, {})
  if (settings && !settings.onboarding_done) {
    return jsx('div', {
      className: 'flex h-full min-h-0 flex-col p-4',
      children: jsx(Onboarding, {})
    })
  }
  const notesOn = !!(settings && settings.notes_enabled)
  const tabs = notesOn ? TABS : TABS.filter((x) => x.id !== 'notes')
  const views = { facts: FactsView, notes: NotesView, core: CoreView, watch: WatchView, injections: InjectionsView, graphs: GraphsView, stats: StatsView, settings: SettingsView, about: AboutView }
  const View = views[tab] || FactsView

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-3',
        children: [
          jsxs('div', {
            className: 'flex flex-col',
            children: [
              jsx('span', { className: 'text-sm font-medium text-(--ui-text-primary)', children: t('dashboard.nav.label') }),
              jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: t('dashboard.page.subtitle') })
            ]
          }),
          jsx(Button, {
            size: 'sm', variant: 'ghost',
            onClick: () => { void refresh() },
            children: t('dashboard.page.refresh')
          })
        ]
      }),
      jsx(SegmentedControl, { value: notesOn ? tab : (tab === 'notes' ? 'facts' : tab), onChange: (v) => tab$.set(v), options: labelled(tabs) }),
      jsx('div', { className: 'flex min-h-0 flex-1 flex-col', children: jsx(View, {}) })
    ]
  })
}

// Plugin stylesheet. The app's CSS is compiled from the core sources: a Tailwind
// class written here does not exist in the bundle and never applies. These rules
// therefore reproduce, in plain CSS, those of the native dialog (DialogContent /
// DialogTitle / Button) and read the same theme variables — hence the impression
// of an app screen, not a bolted-on panel.
const STYLE_ID = 'carnet-styles'

const STYLESHEET = `
.carnet-head-val.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.carnet-head-text {
  display: flex;
  align-items: flex-start;
  gap: 0.375rem;
}
.carnet-head-val {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 0.75rem;
  line-height: 1.4;
  color: var(--ui-text-secondary, #b3b3bb);
}
.carnet-head-text:hover .carnet-head-pencil { opacity: 1; }
.carnet-head-edit {
  display: flex;
  align-items: flex-start;
  gap: 0.375rem;
}
.carnet-detail {
  display: flex;
  flex-direction: column;
  gap: 0.0625rem;
  margin-top: 0.25rem;
  padding: 0.25rem 0.625rem 0.5rem;
  border-top: 1px solid var(--stroke-nous, rgb(255 255 255 / 7%));
}
.carnet-field {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
  padding: 0.125rem 0.25rem;
  border-radius: 2.5px;
}
.carnet-field:hover { background: var(--chrome-action-hover, rgb(255 255 255 / 4%)); }
.carnet-field-key {
  flex: 0 0 6.5rem;
  padding-top: 0.0625rem;
  font-size: 0.6875rem;
  color: var(--ui-text-tertiary, #9a9aa2);
}
.carnet-field-value {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  align-items: flex-start;
  gap: 0.375rem;
}
.carnet-field-val {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 0.75rem;
  line-height: 1.45;
  color: var(--ui-text-secondary, #b3b3bb);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.carnet-field-val.empty { color: var(--ui-text-quaternary, #6d6d76); }
.carnet-field-val.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.6875rem;
}
.carnet-field-pencil {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1rem;
  height: 1rem;
  padding: 0;
  border: 0;
  border-radius: 2.5px;
  background: transparent;
  color: var(--ui-text-quaternary, #6d6d76);
  font-size: 0.6875rem;
  cursor: pointer;
  opacity: 0;
  transition: opacity 100ms, background-color 100ms, color 100ms;
}
.carnet-field:hover .carnet-field-pencil,
.carnet-field-pencil:focus-visible { opacity: 1; }
.carnet-field-pencil:hover {
  background: var(--chrome-action-hover, rgb(255 255 255 / 6%));
  color: var(--ui-text-primary, #e8e8ea);
}
.carnet-field-edit {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  align-items: center;
  gap: 0.375rem;
}
.carnet-field-input { display: flex; flex: 1 1 auto; min-width: 0; }
.carnet-field-actions { display: flex; flex: 0 0 auto; gap: 0.25rem; }
.carnet-input {
  flex: 1 1 auto;
  min-width: 0;
  box-sizing: border-box;
  padding: 0.1875rem 0.375rem;
  border: 1px solid var(--stroke-nous, rgb(255 255 255 / 10%));
  border-radius: 2.5px;
  background: var(--ui-bg-quaternary, rgb(255 255 255 / 4%));
  color: var(--ui-text-primary, #e8e8ea);
  font: inherit;
  font-size: 0.75rem;
  line-height: 1.45;
  outline: none;
}
.carnet-input:focus { border-color: var(--color-ring, #0053fd); }
textarea.carnet-input { min-height: 3rem; resize: vertical; }
.carnet-input-meta {
  flex: 0 1 auto;
  width: auto;
  height: 1.375rem;
  padding: 0 0.25rem;
  font-size: 0.6875rem;
}
.carnet-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.125rem 0.375rem;
  padding: 0.25rem 0.25rem 0.375rem;
}
.carnet-meta-slot { display: inline-flex; align-items: center; gap: 0.375rem; }
.carnet-meta-group { display: inline-flex; align-items: baseline; gap: 0.1875rem; }
.carnet-meta-sep { color: var(--ui-text-quaternary, #6d6d76); font-size: 0.625rem; opacity: 0.7; }
/* "active", "missing", "agent" alone mean nothing: each value in the strip
   carries its label, set back. */
.carnet-meta-key {
  color: var(--ui-text-quaternary, #6d6d76);
  font-size: 0.625rem;
  letter-spacing: 0.01em;
}
.carnet-meta-item {
  border: 0;
  background: transparent;
  padding: 0.0625rem 0.1875rem;
  border-radius: 2.5px;
  font: inherit;
  font-size: 0.6875rem;
  color: var(--ui-text-secondary, #b3b3bb);
  cursor: pointer;
}
.carnet-meta-item:hover {
  background: var(--chrome-action-hover, rgb(255 255 255 / 6%));
  color: var(--ui-text-primary, #e8e8ea);
}
.carnet-meta-item.empty { color: var(--ui-text-quaternary, #6d6d76); }
.carnet-meta-edit { display: inline-flex; }
.carnet-proposal {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  margin: 0.5rem 0 0.25rem;
  padding: 0.5rem 0.625rem;
  border: 1px solid var(--stroke-nous, rgb(255 255 255 / 8%));
  border-radius: 2.5px;
  background: var(--ui-bg-quaternary, rgb(255 255 255 / 3%));
}
.carnet-proposal-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem;
}
.carnet-proposal-title {
  font-size: 0.6875rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--ui-text-tertiary, #9a9aa2);
}
.carnet-proposal-length {
  font-size: 0.625rem;
  font-variant-numeric: tabular-nums;
  color: var(--ui-text-quaternary, #6d6d76);
}
.carnet-proposal-length.over { color: var(--destructive, #e5484d); }
.carnet-proposal-text {
  font-size: 0.75rem;
  line-height: 1.45;
  color: var(--ui-text-primary, #e8e8ea);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.carnet-proposal-foot {
  display: flex;
  align-items: center;
  gap: 0.375rem;
}
.carnet-proposal-note {
  font-size: 0.625rem;
  color: var(--destructive, #e5484d);
}
.carnet-detail-foot {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  margin-top: 0.375rem;
}
.carnet-discreet {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  border: 0;
  background: transparent;
  padding: 0.125rem 0.375rem;
  border-radius: 2.5px;
  font: inherit;
  font-size: 0.6875rem;
  color: var(--ui-text-tertiary, #9a9aa2);
  cursor: pointer;
}
.carnet-discreet:hover {
  background: var(--chrome-action-hover, rgb(255 255 255 / 6%));
  color: var(--ui-text-primary, #e8e8ea);
}
.carnet-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.625rem;
  color: var(--ui-text-quaternary, #6d6d76);
  padding: 0 0.25rem;
}
.carnet-link-row {
  display: block;
  width: 100%;
  box-sizing: border-box;
  text-align: left;
  padding: 2px 4px;
  border-radius: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.6875rem;
  line-height: 1.35;
  color: var(--ui-text-tertiary, #9a9aa2);
  background: transparent;
  border: 0;
  cursor: pointer;
}
.carnet-link-row:hover {
  background: var(--ui-bg-hover, rgb(255 255 255 / 6%));
  color: var(--ui-text-secondary, #c8c8cc);
}

.carnet-mini {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.375rem;
  height: 1.375rem;
  padding: 0;
  border: 0;
  border-radius: 2.5px;
  font-size: 0.75rem;
  cursor: pointer;
}
.carnet-mini-confirm { background: var(--color-primary, #0053fd); color: var(--color-primary-foreground, #fff); }
.carnet-mini-cancel { background: transparent; color: var(--ui-text-tertiary, #9a9aa2); }.carnet-mini-cancel:hover { background: var(--chrome-action-hover, rgb(255 255 255 / 6%)); color: var(--ui-text-primary, #e8e8ea); }
.carnet-status {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  height: 1.25rem;
  padding: 0 0.35rem;
  color: var(--ui-text-secondary, #b3b3bb);
  font-size: 0.625rem;
  font-variant-numeric: tabular-nums;
  line-height: 1;
  user-select: none;
}
.carnet-status svg { flex: none; }
.carnet-status-count { color: var(--ui-text-tertiary, #9a9aa2); }

/* Onboarding: one question per screen, a lot of air, one main action. */
.carnet-ob {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.carnet-ob-content {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  padding: 2rem 1rem 1rem;
}
.carnet-ob-center {
  margin: auto;
  max-width: 34rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 0.9rem;
}
.carnet-ob-stack {
  margin: auto;
  width: 100%;
  max-width: 34rem;
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}
.carnet-ob-stack-tight {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.carnet-ob-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--ui-text-primary, #f2f2f4);
  letter-spacing: -0.01em;
}
.carnet-ob-lead {
  font-size: 0.8125rem;
  line-height: 1.55;
  color: var(--ui-text-secondary, #b3b3bb);
}
.carnet-ob-layers {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin: 0.25rem 0;
}
.carnet-ob-layer {
  padding: 0.55rem 0.75rem;
  border: 1px solid var(--stroke-nous, rgb(255 255 255 / 7%));
  border-radius: 0.5rem;
  background: var(--surface-raised, rgb(255 255 255 / 2%));
  font-size: 0.75rem;
  line-height: 1.45;
  color: var(--ui-text-secondary, #b3b3bb);
  text-align: left;
}
.carnet-ob-promise {
  font-size: 0.75rem;
  color: var(--ui-text-tertiary, #8a8a93);
}
.carnet-ob-cmd {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.75rem;
  padding: 0.5rem 0.75rem;
  border-radius: 0.4rem;
  border: 1px solid var(--stroke-nous, rgb(255 255 255 / 8%));
  background: var(--surface-raised, rgb(255 255 255 / 2%));
  color: var(--ui-text-primary, #f2f2f4);
  user-select: all;
}
.carnet-ob-cta {
  min-width: 11rem;
  justify-content: center;
  padding-left: 1.5rem;
  padding-right: 1.5rem;
}
.carnet-ob-skip {
  background: none;
  border: 0;
  padding: 0;
  font-size: 0.6875rem;
  color: var(--ui-text-tertiary, #8a8a93);
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
.carnet-ob-skip:hover { color: var(--ui-text-secondary, #b3b3bb); }
.carnet-ob-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.4rem;
}
.carnet-ob-choice {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  padding: 0.6rem 0.7rem;
  text-align: left;
  border: 1px solid var(--stroke-nous, rgb(255 255 255 / 7%));
  border-radius: 0.5rem;
  background: var(--surface-raised, rgb(255 255 255 / 2%));
  cursor: pointer;
  transition: border-color 120ms ease, background 120ms ease;
}
.carnet-ob-choice:hover { border-color: var(--stroke-strong, rgb(255 255 255 / 16%)); }
.carnet-ob-choice.is-active {
  border-color: var(--accent, #6ee7a8);
  background: color-mix(in srgb, var(--accent, #6ee7a8) 8%, transparent);
}
.carnet-ob-choice-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
.carnet-ob-choice-title {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--ui-text-primary, #f2f2f4);
}
.carnet-ob-choice-hint {
  font-size: 0.6875rem;
  line-height: 1.4;
  color: var(--ui-text-tertiary, #8a8a93);
}
.carnet-ob-warn {
  font-size: 0.625rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--ui-warning, #e0a458);
  border: 1px solid currentColor;
  border-radius: 999px;
  padding: 0.05rem 0.35rem;
  opacity: 0.9;
  flex: 0 0 auto;
}
.carnet-ob-choice.is-warn { border-color: color-mix(in srgb, var(--ui-warning, #e0a458) 45%, transparent); }
.carnet-ob-choice.is-warn .carnet-ob-choice-hint { color: color-mix(in srgb, var(--ui-warning, #e0a458) 75%, var(--ui-text-tertiary, #8a8a93)); }
.carnet-ob-dot {
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 999px;
  border: 1px solid var(--stroke-strong, rgb(255 255 255 / 22%));
  flex: 0 0 auto;
}
.carnet-ob-dot.is-on {
  border-color: var(--accent, #6ee7a8);
  background: var(--accent, #6ee7a8);
}
.carnet-ob-fields {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.carnet-ob-field-label {
  font-size: 0.6875rem;
  color: var(--ui-text-tertiary, #8a8a93);
}
.carnet-ob-download {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  padding-top: 0.15rem;
}
.carnet-ob-bar {
  height: 3px;
  border-radius: 999px;
  background: var(--stroke-nous, rgb(255 255 255 / 8%));
  overflow: hidden;
}
.carnet-ob-bar i,
.carnet-ob-progress i {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--accent, #6ee7a8);
  transition: width 200ms ease;
}
.carnet-ob-summary {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--stroke-nous, rgb(255 255 255 / 7%));
  border-radius: 0.5rem;
  overflow: hidden;
}
.carnet-ob-sum-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.5rem 0.75rem;
  font-size: 0.75rem;
  color: var(--ui-text-secondary, #b3b3bb);
}
.carnet-ob-sum-row + .carnet-ob-sum-row {
  border-top: 1px solid var(--stroke-nous, rgb(255 255 255 / 6%));
}
.carnet-ob-sum-row strong {
  font-weight: 500;
  color: var(--ui-text-primary, #f2f2f4);
  font-variant-numeric: tabular-nums;
}
.carnet-ob-note {
  font-size: 0.6875rem;
  color: var(--ui-text-tertiary, #8a8a93);
}
.carnet-ob-foot {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding-top: 0.5rem;
}
.carnet-ob-progress {
  height: 2px;
  border-radius: 999px;
  background: var(--stroke-nous, rgb(255 255 255 / 8%));
  overflow: hidden;
}
.carnet-ob-foot-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.carnet-ob-count {
  flex: 1 1 auto;
  font-size: 0.6875rem;
  color: var(--ui-text-tertiary, #8a8a93);
}
.carnet-ob-error {
  padding: 0 1rem 0.5rem;
  font-size: 0.75rem;
  color: var(--ui-danger, #f87171);
}
`

function mountStylesheet() {
  if (typeof document === 'undefined') return
  // Rewrite the content on every load: a simple "already there?" left the
  // previous version's stylesheet in place after a plugin reload, and rules
  // added since then never applied.
  let el = document.getElementById(STYLE_ID)
  if (!el) {
    el = document.createElement('style')
    el.id = STYLE_ID
    document.head.appendChild(el)
  }
  el.textContent = STYLESHEET
}


export default {
  id: ID,
  name: 'Carnet',
  // Strings are fetched once before registering: the nav and palette labels are
  // plain strings, read at registration time. A failed fetch leaves t() on its
  // key fallback rather than blocking the page.
  async register(ctx) {
    _ctx = ctx
    mountStylesheet()
    await loadStrings().catch(() => {})
    const paletteLabel = t('dashboard.palette.open') === 'dashboard.palette.open'
      ? 'Open Carnet'
      : t('dashboard.palette.open')
    const settings = await probeBackend()
    if (settings) {
      modelMode$.set(['custom', 'lmstudio'].includes(settings.model_mode) ? settings.model_mode : 'off')
    }
    return ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/carnet' }, render: () => jsx(CarnetPage, {}) },
      // A product name does not translate, and this label must survive a backend
      // that cannot serve the strings yet.
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/carnet', label: 'Carnet', codicon: 'book' } },
      {
        id: 'open', area: PALETTE_AREA,
        data: {
          id: 'carnet.open',
          label: paletteLabel,
          keywords: ['memory', 'facts', 'watch'],
          run: () => host.navigate('/carnet')
        }
      }
    ])
  }
}
