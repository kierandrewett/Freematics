import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ArrowUpRight, Search } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

type Trip = { device_id: string; trip_id: string; sample_count: number; moving_readings: number; gap_count: number; distance_km: number | null; has_report: boolean; start_capture_ms: number | null; timeline_start_ms: number | null; timeline_end_ms: number | null }
type Fault = { code: string; interpretation: string; possible_causes: string[]; evidence_for: string[]; evidence_against: string[]; next_checks: string[]; source_urls: string[] }
type Report = { summary: string; patterns: string[]; possible_issues: string[]; data_limits: string[]; fault_analysis: Fault[] }
type Saved = { report?: Report | null; screen?: { decision?: string }; attempts?: number; requested_model?: string; returned_model?: string; analyzed_at_ms?: number }
type Detail = { codes: { status: string; code: string }[]; report: Saved | null }
type Status = { device_id: string; trip_count: number; latest?: { archive_mtime_ms?: number }; completed_reports: number; report_version: number; auth?: { account?: { type?: string }; error?: string; login?: { verificationUrl: string; userCode: string } } }

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 })
const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "long", year: "numeric" })
const groupFormat = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" })
const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" })
const stampFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })

async function api<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`)
  return body as T
}
function tripDate(trip: Trip): Date | null {
  const timestamp = trip.start_capture_ms || trip.timeline_start_ms
  if (timestamp && timestamp > 1500000000000) return new Date(timestamp)
  const id = trip.trip_id
  return /^\d{8}-\d{6}$/.test(id) ? new Date(`${id.slice(0, 4)}-${id.slice(4, 6)}-${id.slice(6, 8)}T${id.slice(9, 11)}:${id.slice(11, 13)}:${id.slice(13, 15)}Z`) : null
}
function tripTime(trip: Trip): string {
  const date = tripDate(trip)
  if (!date) return "Time unknown"
  return trip.start_capture_ms ? timeFormat.format(date) : `${trip.trip_id.slice(9, 11)}:${trip.trip_id.slice(11, 13)} UTC ≈`
}
function duration(trip: Trip): string | null {
  if (!trip.timeline_start_ms || !trip.timeline_end_ms) return null
  const minutes = Math.round((trip.timeline_end_ms - trip.timeline_start_ms) / 60000)
  return minutes < 1 ? "<1 min" : `${minutes} min`
}
function isJourney(trip: Trip): boolean {
  return trip.moving_readings >= 20 || (trip.distance_km !== null && trip.distance_km >= 0.2)
}
function grafana(trip: Trip): string | null {
  if (!trip.sample_count || !trip.timeline_start_ms || !trip.timeline_end_ms) return null
  const params = new URLSearchParams({ "var-device": trip.device_id, "var-trip": trip.trip_id, from: String(trip.timeline_start_ms - 30000), to: String(trip.timeline_end_ms + 30000) })
  return `https://grafana.drewett.dev/d/freematics-trips?${params}`
}
function grouped(trips: Trip[]): { label: string; trips: Trip[] }[] {
  const result: { label: string; trips: Trip[] }[] = []
  for (const trip of trips) {
    const date = tripDate(trip)
    const label = date ? groupFormat.format(date) : "Date unknown"
    const last = result[result.length - 1]
    if (last?.label === label) last.trips.push(trip)
    else result.push({ label, trips: [trip] })
  }
  return result
}
function TripRows({ trips, selected, onSelect }: { trips: Trip[]; selected: string | null; onSelect: (id: string) => void }) {
  return grouped(trips).map(group => <section className="trip-group" key={`${group.label}-${group.trips[0].trip_id}`}>
    <h3 className="trip-group-label">{group.label}</h3>
    {group.trips.map(trip => <button type="button" className={cn("trip-row", selected === trip.trip_id && "trip-row-selected")} key={trip.trip_id} aria-current={selected === trip.trip_id ? "true" : undefined} onClick={() => onSelect(trip.trip_id)}>
      <span className="trip-row-top"><strong>{tripTime(trip)}</strong><span>{duration(trip) || "—"}</span></span>
      <span className="trip-row-bottom"><span>{number.format(trip.sample_count)} readings{trip.distance_km != null ? ` · ${number.format(trip.distance_km)} km` : ""}</span>{trip.has_report && <span>Reviewed</span>}</span>
    </button>)}
  </section>)
}
function Notes({ title, entries, numbered = false }: { title: string; entries?: string[]; numbered?: boolean }) {
  if (!entries?.length) return null
  return <section className="report-section"><h3>{title}</h3>{numbered
    ? <ol className="observation-list">{entries.map((entry, i) => <li key={`${i}-${entry}`}><span aria-hidden="true">{String(i + 1).padStart(2, "0")}</span><p>{entry}</p></li>)}</ol>
    : <ul className="note-list">{entries.map((entry, i) => <li key={`${i}-${entry}`}>{entry}</li>)}</ul>}</section>
}
function Assessment({ saved }: { saved: Saved | null }) {
  const report = saved?.report
  if (!report) return <div className="assessment-empty"><h3>{saved?.screen?.decision === "no_report" ? "No report trigger" : "Review pending"}</h3><p>{saved?.screen?.decision === "no_report" ? "The available readings did not cross a review threshold. Explore the full trip data in Grafana." : saved?.attempts ? `The mechanic is retrying this review (attempt ${saved.attempts}).` : "This trip is waiting for its initial screen."} Recorded data remains available in Grafana.</p></div>
  return <div className="assessment">
    <div className="assessment-lead"><span className="section-kicker">MECHANIC ASSESSMENT</span><p>{report.summary}</p></div>
    <Notes title="What stood out" entries={report.patterns} numbered />
    <Notes title="Worth checking" entries={report.possible_issues} numbered />
    {report.fault_analysis?.map(fault => <details className="fault-report" key={fault.code}><summary className="fault-heading"><Badge variant="outline" className="font-mono">{fault.code}</Badge><strong>{fault.interpretation}</strong><span>Evidence and next checks</span></summary><div className="fault-detail"><Notes title="Possible causes" entries={fault.possible_causes} /><Notes title="Evidence for" entries={fault.evidence_for} /><Notes title="Evidence against" entries={fault.evidence_against} /><Notes title="Next checks" entries={fault.next_checks} />{!!fault.source_urls?.length && <div className="fault-sources">{fault.source_urls.filter(url => url.startsWith("https://")).map((url, i) => <a href={url} key={url} target="_blank" rel="noopener noreferrer">Reference {i + 1} <ArrowUpRight aria-hidden="true" /></a>)}</div>}</div></details>)}
    {!!report.data_limits?.length && <details className="evidence-limits"><summary>Evidence limits <span>{report.data_limits.length}</span></summary><ul className="note-list">{report.data_limits.map((entry, i) => <li key={`${i}-${entry}`}>{entry}</li>)}</ul></details>}
  </div>
}

function App() {
  const [trips, setTrips] = useState<Trip[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [showOther, setShowOther] = useState(false)
  const [loading, setLoading] = useState(false)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [statusCheckedAt, setStatusCheckedAt] = useState<number | null>(null)
  const [listError, setListError] = useState("")
  const [detailError, setDetailError] = useState("")
  const selectedRef = useRef<string | null>(null)
  const reportCount = useRef(0)
  const current = trips.find(trip => trip.trip_id === selected) || null
  const query = search.trim().toLowerCase()
  const visible = useMemo(() => trips.filter(trip => { const date = tripDate(trip); return !query || trip.trip_id.toLowerCase().includes(query) || !!date && (date.toLocaleDateString().toLowerCase().includes(query) || dateFormat.format(date).toLowerCase().includes(query) || groupFormat.format(date).toLowerCase().includes(query)) }), [trips, query])
  const journeys = visible.filter(isJourney)
  const other = visible.filter(trip => !isJourney(trip))
  const otherExpanded = showOther || !!query || !!selected && other.some(trip => trip.trip_id === selected)

  const selectTrip = useCallback((id: string) => {
    selectedRef.current = id
    setSelected(id)
    setDetail(null)
    setDetailError("")
  }, [])
  const loadTrips = useCallback(async (before?: string | null, showLoading = true) => {
    if (showLoading) { setLoading(true); setListError("") }
    try {
      const result = await api<{ trips: Trip[]; next_before: string | null }>(`/api/ui/trips?limit=150${before ? `&before=${encodeURIComponent(before)}` : ""}`)
      setTrips(old => { const seen = new Set(old.map(trip => trip.trip_id)); return [...old, ...result.trips.filter(trip => !seen.has(trip.trip_id))] }); setNext(result.next_before)
      if (!selectedRef.current) { const first = result.trips.find(isJourney) || result.trips.find(trip => trip.sample_count > 0) || result.trips[0]; if (first) selectTrip(first.trip_id) }
    } catch (cause) { setListError(`Could not load trips: ${String(cause)}`) }
    finally { if (showLoading) setLoading(false) }
  }, [selectTrip])
  const refresh = useCallback(async () => {
    try {
      const result = await api<Status>("/api/ui/status"); setStatus(result); setStatusCheckedAt(Date.now())
      if (result.report_version > reportCount.current) {
        const overview = await api<{ trips: Trip[] }>("/api/ui/trips?limit=150")
        const fresh = new Map(overview.trips.map(trip => [trip.trip_id, trip]))
        setTrips(old => old.map(trip => fresh.get(trip.trip_id) || trip))
        if (selectedRef.current) { const trip = selectedRef.current; const latest = await api<{ report: Saved | null }>(`/api/ui/report?trip=${encodeURIComponent(trip)}`); if (selectedRef.current === trip) setDetail(old => old ? { ...old, report: latest.report } : old) }
      }
      reportCount.current = result.report_version
    } catch { /* Trip archive remains available if the status request fails. */ }
  }, [])
  useEffect(() => { const initial = window.setTimeout(() => { void loadTrips(undefined, false); void refresh() }, 0); const timer = setInterval(() => void refresh(), 30000); return () => { window.clearTimeout(initial); clearInterval(timer) } }, [loadTrips, refresh])
  useEffect(() => { if (!selected) return; let cancelled = false; api<Detail>(`/api/ui/trip?id=${encodeURIComponent(selected)}&limit=1`).then(result => { if (!cancelled) setDetail(result) }).catch(cause => { if (!cancelled) setDetailError(`Could not load this report: ${String(cause)}`) }); return () => { cancelled = true } }, [selected])

  const last = status?.latest?.archive_mtime_ms
  const link = current ? grafana(current) : null
  const date = current ? tripDate(current) : null
  return <div className="app-shell">
    <header className="topbar"><div className="topbar-brand"><strong>FREEMATICS</strong><span>Vehicle record</span></div><div className="topbar-right"><span className="receipt-status" role="status"><span className={cn("status-dot", last && statusCheckedAt && statusCheckedAt - last < 600000 && "status-dot-recent")} />{status ? `${status.device_id} · ${last ? `Last data ${stampFormat.format(new Date(last))}` : "No indexed data"}` : "Checking latest data"}</span><Button size="sm" variant="ghost" asChild><a href="https://grafana.drewett.dev/d/freematics-vehicle?var-device=ZKUCALJ0">Grafana <ArrowUpRight data-icon="inline-end" /></a></Button></div></header>
    <div className="workspace">
      <aside className="archive-panel" aria-label="Trip archive"><div className="archive-head"><div className="archive-title"><h1>Trip archive</h1><span>{status?.trip_count ?? trips.length} total</span></div><label className="search-field"><Search aria-hidden="true" /><Input aria-label="Search trips by date or ID" placeholder="Search date or trip ID" value={search} onChange={event => setSearch(event.target.value)} /></label></div>
        <ScrollArea className="archive-scroll"><div className="archive-list">
          {listError && <Alert variant="destructive" className="m-3"><AlertTitle>Archive unavailable</AlertTitle><AlertDescription>{listError} <Button size="xs" variant="outline" onClick={() => void loadTrips()}>Retry</Button></AlertDescription></Alert>}
          {loading && !trips.length && <div className="archive-loading"><Skeleton className="h-10" /><Skeleton className="h-10" /><Skeleton className="h-10" /></div>}
          {!!journeys.length && <section className="archive-category"><h2>Journeys <span>{journeys.length}</span></h2><TripRows trips={journeys} selected={selected} onSelect={selectTrip} /></section>}
          {!!other.length && <section className="archive-category"><button type="button" className="category-toggle" aria-expanded={otherExpanded} onClick={() => setShowOther(value => !value)}>Other sessions <span>{other.length}</span><span aria-hidden="true">{otherExpanded ? "−" : "+"}</span></button>{otherExpanded && <TripRows trips={other} selected={selected} onSelect={selectTrip} />}</section>}
          {!loading && !visible.length && !listError && <p className="archive-empty">{query ? "No sessions match this search." : "No trips have been recorded yet."}</p>}
          {next && <div className="load-older"><Button size="sm" variant="outline" disabled={loading} onClick={() => void loadTrips(next)}>{loading ? "Loading…" : "Load older trips"}</Button></div>}
        </div></ScrollArea><div className="archive-foot">≈ Time from collector when GPS time is unavailable</div></aside>
      <main className="trip-panel">{current ? <>
        <div className="trip-heading"><div><span className="section-kicker">TRIP RECORD</span><h2>{date ? dateFormat.format(date) : current.trip_id}</h2><p>{tripTime(current)} <span aria-hidden="true">·</span> <code>{current.trip_id}</code>{!current.start_capture_ms && <span> · Time approximate</span>}</p></div>{link && <Button variant="outline" size="sm" asChild><a href={link}>Explore data in Grafana <ArrowUpRight data-icon="inline-end" /></a></Button>}</div>
        <div className="fact-strip" aria-label="Trip summary"><div><span>Duration</span><strong>{duration(current) || "Unknown"}</strong></div><div><span>GPS route</span><strong>{current.distance_km == null ? "Unavailable" : `${number.format(current.distance_km)} km`}</strong></div><div><span>Readings</span><strong>{number.format(current.sample_count)}</strong></div><div><span>Data gaps</span><strong>{number.format(current.gap_count)}</strong></div></div>
        <div className="trip-content">{detailError && <Alert variant="destructive"><AlertTitle>Report unavailable</AlertTitle><AlertDescription>{detailError}</AlertDescription></Alert>}{!!detail?.codes?.length && <section className="codes-line"><h3>Fault codes</h3><div>{detail.codes.map(code => <Badge key={`${code.status}-${code.code}`} variant="outline" className="font-mono">{code.status} {code.code}</Badge>)}</div></section>}{!detail && !detailError ? <div className="report-loading"><Skeleton className="h-5 w-3/4" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-5/6" /><Skeleton className="h-4 w-2/3" /></div> : <Assessment saved={detail?.report || null} />}{detail?.report?.report && <div className="report-provenance">Reviewed {detail.report.analyzed_at_ms ? stampFormat.format(new Date(detail.report.analyzed_at_ms)) : "at an unknown time"} · {detail.report.returned_model || detail.report.requested_model || "Model unknown"}</div>}</div>
      </> : <div className="no-selection"><h2>Select a trip</h2><p>Choose a journey from the archive to read its mechanic assessment and open its recorded data in Grafana.</p></div>}
      {status && status.auth?.account?.type !== "chatgpt" && <Alert className="auth-alert"><AlertTitle>Mechanic sign-in needed</AlertTitle><AlertDescription className="flex flex-col gap-2"><span>New assessments need the server-side ChatGPT connection. Collection continues.</span>{status.auth?.error && <span>{status.auth.error}</span>}{status.auth?.login ? <span>Open <a className="underline" href={status.auth.login.verificationUrl} target="_blank" rel="noopener noreferrer">{status.auth.login.verificationUrl}</a> and enter <strong className="font-mono">{status.auth.login.userCode}</strong>.</span> : <Button className="w-fit" size="sm" onClick={async () => { await fetch("/login", { method: "POST", credentials: "same-origin", redirect: "manual" }); await refresh() }}>Sign in to ChatGPT</Button>}</AlertDescription></Alert>}</main>
    </div>
  </div>
}
export default App
