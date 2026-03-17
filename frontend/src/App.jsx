import React, { useMemo, useState } from "react";

const API_BASE = "https://chiller-diagnostic-assistant.onrender.com";

const snapshotFields = [
  ["suction_pressure", "Suction Pressure", "number"],
  ["discharge_pressure", "Discharge Pressure", "number"],
  ["superheat", "Superheat", "number"],
  ["subcooling", "Subcooling", "number"],
  ["leaving_temp", "Leaving Fluid Temperature", "number"],
  ["return_temp", "Return Fluid Temperature", "number"],
  ["flow_rate", "Flow Rate", "number"],
  ["pump_amps", "Pump Amps", "number"],
  ["ambient_temp", "Ambient Temperature", "number"],
  ["glycol_percent", "Glycol %", "number"],
  ["flow_confirmed", "Flow Confirmed", "boolean"],
  ["fans_running", "Condenser Fans Running", "boolean"],
  ["compressor_running", "Compressor Running", "boolean"],
];

function inputFor(type, value, onChange) {
  if (type === "boolean") {
    return (
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">Select</option><option value="yes">Yes</option><option value="no">No</option>
      </select>
    );
  }
  return <input type="number" value={value ?? ""} onChange={(e) => onChange(e.target.value)} placeholder="Enter value" />;
}

function questionInput(question, value, onChange) {
  if (question.input_type === "boolean") {
    return (
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">Select</option><option value="yes">Yes</option><option value="no">No</option>
      </select>
    );
  }
  if (question.input_type === "number") {
    return <input type="number" value={value ?? ""} onChange={(e) => onChange(e.target.value)} placeholder={question.unit ? `Enter value (${question.unit})` : "Enter value"} />;
  }
  return <input type="text" value={value ?? ""} onChange={(e) => onChange(e.target.value)} placeholder="Type answer" />;
}

function buildServiceSummary(matchResult, diagnosis, answers, snapshotValues, metrics, noteText) {
  const obs = Object.entries(snapshotValues || {}).filter(([, v]) => v !== "" && v != null).map(([k, v]) => `- ${k}: ${v}`).join("\n");
  const ans = Object.entries(answers || {}).filter(([, v]) => v !== "" && v != null).map(([k, v]) => `- ${k}: ${v}`).join("\n");
  const mets = Object.entries(metrics || {}).map(([k, v]) => `- ${k}: ${v}`).join("\n");
  const topCauses = (diagnosis?.results || []).map((r, i) => `${i + 1}. ${r.cause_name} (${r.confidence}, score ${r.score})`).join("\n");
  const actions = (diagnosis?.results?.[0]?.actions || []).map((a) => `- ${a}`).join("\n");
  return `Service Summary

Matched fault family:
${matchResult?.fault_family_name || "N/A"}

Technician notes:
${noteText || "N/A"}

Observed snapshot inputs:
${obs || "N/A"}

Calculated metrics:
${mets || "N/A"}

Diagnostic answers:
${ans || "N/A"}

Top likely causes:
${topCauses || "N/A"}

Recommended actions:
${actions || "N/A"}`.trim();
}

export default function App() {
  const [mode, setMode] = useState("alarm");
  const [alarmText, setAlarmText] = useState("");
  const [techNote, setTechNote] = useState("");
  const [snapshotValues, setSnapshotValues] = useState({});
  const [guidedValues, setGuidedValues] = useState({});
  const [matchResult, setMatchResult] = useState(null);
  const [answers, setAnswers] = useState({});
  const [diagnosis, setDiagnosis] = useState(null);
  const [serviceSummary, setServiceSummary] = useState("");
  const [feedbackForm, setFeedbackForm] = useState({ actual_root_cause: "", was_prediction_correct: "yes", technician_notes: "" });
  const [feedbackStatus, setFeedbackStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [noteDiagnosis, setNoteDiagnosis] = useState(null);

  const canDiagnose = useMemo(() => matchResult && matchResult.questions && matchResult.questions.length > 0, [matchResult]);

  async function postJson(url, body) {
    const res = await fetch(`${API_BASE}${url}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed.");
    return data;
  }

  async function handleMatchAlarm(e) {
    e.preventDefault();
    setLoading(true); setError(""); setDiagnosis(null); setServiceSummary("");
    try {
      const data = await postJson("/match-alarm", { alarm_text: alarmText });
      setMatchResult(data); setAnswers({});
    } catch (err) { setMatchResult(null); setError(err.message || "Something went wrong."); }
    finally { setLoading(false); }
  }

  async function handleParseTechNote() {
    setLoading(true); setError("");
    try {
      const data = await postJson("/parse-tech-note", { note_text: techNote });
      setSnapshotValues((prev) => ({ ...prev, ...data.extracted }));
      setMode("snapshot");
    } catch (err) { setError(err.message || "Something went wrong."); }
    finally { setLoading(false); }
  }

  async function handleSnapshotMatch(e) {
    e.preventDefault();
    setLoading(true); setError(""); setDiagnosis(null); setServiceSummary("");
    try {
      const payload = {};
      for (const [key, value] of Object.entries(snapshotValues)) {
        if (value === "") continue;
        payload[key] = ["flow_confirmed", "fans_running", "compressor_running"].includes(key) ? value : Number(value);
      }
      const data = await postJson("/snapshot-match", payload);
      setMatchResult(data); setAnswers({});
    } catch (err) { setMatchResult(null); setError(err.message || "Something went wrong."); }
    finally { setLoading(false); }
  }

  async function handleGuidedMatch(e) {
    e.preventDefault();
    setLoading(true); setError(""); setDiagnosis(null); setServiceSummary("");
    try {
      const data = await postJson("/guided-troubleshoot", guidedValues);
      setMatchResult({ ...data, mode: "guided", description: "Guided troubleshooting routed you into this fault family." });
      setAnswers({});
    } catch (err) { setError(err.message || "Something went wrong."); }
    finally { setLoading(false); }
  }

async function handleAutoDiagnoseNote() {
  setLoading(true);
  setError("");

  try {
    const res = await fetch(`${API_BASE}/auto-diagnose-note`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ note_text: techNote }),
    });

    const data = await res.json();

    if (!res.ok) throw new Error(data.detail || "Auto diagnose failed");

    setSnapshotValues(data.extracted);
    setMatchResult(data.snapshot_match);
    setDiagnosis(data.diagnosis);
    setNoteDiagnosis(data);

  } catch (err) {
    setError(err.message);
  } finally {
    setLoading(false);
  }
}

  async function handleDiagnose(e) {
    e.preventDefault();
    if (!matchResult) return;
    setLoading(true); setError("");
    try {
      const normalizedAnswers = {};
      for (const [key, value] of Object.entries(answers)) {
        if (value === "") continue;
        const q = matchResult.questions.find((item) => item.variable_name === key);
        normalizedAnswers[key] = q?.input_type === "number" ? Number(value) : value;
      }
      const data = await postJson("/diagnose", { fault_family_id: matchResult.fault_family_id, answers: normalizedAnswers });
      setDiagnosis(data);
      setServiceSummary(buildServiceSummary(matchResult, data, normalizedAnswers, snapshotValues, matchResult.metrics, techNote));
    } catch (err) { setError(err.message || "Something went wrong."); }
    finally { setLoading(false); }
  }

  async function handleSaveFeedback() {
    setLoading(true); setError(""); setFeedbackStatus("");
    try {
      const topCause = diagnosis?.results?.[0]?.cause_name || "";
      const topFingerprint = matchResult?.fingerprints?.[0]?.name || "";
      const data = await postJson("/feedback", {
        predicted_top_cause: topCause,
        actual_root_cause: feedbackForm.actual_root_cause,
        was_prediction_correct: feedbackForm.was_prediction_correct === "yes",
        technician_notes: feedbackForm.technician_notes,
        fault_family_id: matchResult?.fault_family_id || "",
        matched_fingerprint: topFingerprint,
      });
      setFeedbackStatus(`Saved. Total feedback entries: ${data.count}`);
    } catch (err) { setError(err.message || "Something went wrong."); }
    finally { setLoading(false); }
  }

  async function copySummary() {
    try { await navigator.clipboard.writeText(serviceSummary); } catch {}
  }

  return (
    <div className="app-shell">
      <div className="page">
        <header className="hero">
          <h1>Universal Chiller Diagnostic Assistant</h1>
          <p>Alarm matching, guided troubleshooting, system snapshots, tech-note parsing, fingerprints, and service summaries.</p>
        </header>

        <section className="card">
          <h2>Tech Note Parser</h2>
          <div className="stack">
            <textarea
              rows="4"
              value={techNote}
              onChange={(e) => setTechNote(e.target.value)}
              placeholder='Example: Chiller running but not pulling down. Suction 45 psi. SH 28. SC 3. LWT 55. RWT 63.'
            />
            <button type="button" onClick={handleParseTechNote} disabled={loading || !techNote.trim()}>
              {loading ? "Parsing..." : "Parse Tech Note into Snapshot"}
            </button>
          </div>
        </section><button
  type="button"
  onClick={handleAutoDiagnoseNote}
  disabled={loading || !techNote.trim()}
>
  Auto Diagnose From Tech Note
</button>

        <section className="card">
          <div className="tab-row">
            <button className={mode === "alarm" ? "tab active-tab" : "tab"} type="button" onClick={() => { setMode("alarm"); setMatchResult(null); setDiagnosis(null); setError(""); }}>Diagnose by Alarm</button>
            <button className={mode === "snapshot" ? "tab active-tab" : "tab"} type="button" onClick={() => { setMode("snapshot"); setMatchResult(null); setDiagnosis(null); setError(""); }}>Diagnose by System Snapshot</button>
            <button className={mode === "guided" ? "tab active-tab" : "tab"} type="button" onClick={() => { setMode("guided"); setMatchResult(null); setDiagnosis(null); setError(""); }}>Guided Troubleshooting</button>
          </div>

          {mode === "alarm" ? (
            <form onSubmit={handleMatchAlarm} className="stack">
              <h2>Alarm Input</h2>
              <textarea rows="3" value={alarmText} onChange={(e) => setAlarmText(e.target.value)} placeholder="Example: Flow Switch Open" />
              <button type="submit" disabled={loading || !alarmText.trim()}>{loading ? "Matching..." : "Match Alarm"}</button>
            </form>
          ) : mode === "snapshot" ? (
            <form onSubmit={handleSnapshotMatch} className="stack">
              <h2>System Snapshot</h2>
              <div className="snapshot-grid">
                {snapshotFields.map(([key, label, type]) => (
                  <label key={key} className="field">
                    <span>{label}</span>
                    {inputFor(type, snapshotValues[key] ?? "", (value) => setSnapshotValues((prev) => ({ ...prev, [key]: value })))}
                  </label>
                ))}
              </div>
              <button type="submit" disabled={loading}>{loading ? "Matching..." : "Match Snapshot"}</button>
            </form>
          ) : (
            <form onSubmit={handleGuidedMatch} className="stack">
              <h2>Guided Troubleshooting</h2>
              {[
                ["compressor_running", "Compressor Running"],
                ["pump_running", "Pump Running"],
                ["flow_confirmed", "Flow Confirmed"],
                ["leaving_temp_high", "Leaving Temp High"],
                ["alarm_present", "Alarm Present"],
              ].map(([key, label]) => (
                <label key={key} className="field">
                  <span>{label}</span>
                  <select value={guidedValues[key] ?? ""} onChange={(e) => setGuidedValues((prev) => ({ ...prev, [key]: e.target.value }))}>
                    <option value="">Select</option><option value="yes">Yes</option><option value="no">No</option>
                  </select>
                </label>
              ))}
              <button type="submit" disabled={loading}>{loading ? "Routing..." : "Route to Fault Family"}</button>
            </form>
          )}
        </section>

        {error ? <section className="card error"><strong>Error:</strong> {error}</section> : null}
        {feedbackStatus ? <section className="card success">{feedbackStatus}</section> : null}

        {matchResult ? (
          <section className="grid">
            <div className="card">
              <h2>Matched Fault Family</h2>
              <p className="family-name">{matchResult.fault_family_name}</p>
              <p>{matchResult.description}</p>

              {matchResult.mode === "alarm" ? (
                <>
                  <h3>Top Alias Matches</h3>
                  <ul>{matchResult.matches.map((match, index) => <li key={`${match.alias_text}-${index}`}><strong>{match.alias_text}</strong> <span className="muted">(score {match.score})</span></li>)}</ul>
                </>
              ) : matchResult.mode === "guided" ? (
                <>
                  <h3>Why it routed here</h3>
                  <ul>{(matchResult.why || []).map((line, index) => <li key={index}>{line}</li>)}</ul>
                </>
              ) : (
                <>
                  <h3>Why this family matched</h3>
                  <ul>{(matchResult.why_matched || []).map((line, index) => <li key={index}>{line}</li>)}</ul>

                  <h3>Calculated Metrics</h3>
                  <ul>{Object.entries(matchResult.metrics || {}).map(([k, v]) => <li key={k}><strong>{k}</strong>: {String(v)}</li>)}</ul>

                 <h3>Matched Fingerprints</h3>
{(matchResult.fingerprints || []).length ? (
  <div className="results">
    {matchResult.fingerprints.map((fp) => (
      <article key={fp.id} className="result-card">
        <div className="result-header">
          <h4>{fp.name}</h4>
          <div className="pill-row">
            <span className="pill">
              Match {Math.round((fp.match_score || 0) * 100)}%
            </span>
          </div>
        </div>

        <div className="result-block">
          <h5>Matched Conditions</h5>
          <ul>
            {fp.matched_conditions.map((cond, i) => (
              <li key={i}>
                {cond.field}: {String(cond.actual_value)} matched {cond.operator} {String(cond.target)}
              </li>
            ))}
          </ul>
        </div>

        <div className="result-block">
          <h5>Likely Causes</h5>
          <ul>
            {(fp.likely_causes || []).map((cause, i) => (
              <li key={i}>{cause}</li>
            ))}
          </ul>
        </div>

        <div className="result-block">
          <h5>Next Checks</h5>
          <ul>
            {(fp.next_checks || []).map((check, i) => (
              <li key={i}>{check}</li>
            ))}
          </ul>
        </div>
      </article>
    ))}
  </div>
) : (
  <p className="muted">No strong fingerprint matched; using weighted best guess.</p>
)}

            <div className="card">
              <h2>Targeted Questions</h2>
              {canDiagnose ? (
                <form onSubmit={handleDiagnose} className="stack">
                  {matchResult.questions.map((q) => (
                    <label key={q.id} className="field">
                      <span>{q.question_text}{q.unit ? ` (${q.unit})` : ""}{q.is_required ? " *" : ""}</span>
                      {questionInput(q, answers[q.variable_name] ?? "", (value) => setAnswers((prev) => ({ ...prev, [q.variable_name]: value })))}
                    </label>
                  ))}
                  <button type="submit" disabled={loading}>{loading ? "Diagnosing..." : "Diagnose"}</button>
                </form>
              ) : <p>No questions available for this fault family.</p>}
            </div>
          </section>
        ) : null}

        {diagnosis ? (
          <>
            <section className="card">
              <h2>Results</h2>
              <p className="family-name">{diagnosis.family_name}</p>
              {diagnosis.results.length ? (
                <div className="results">
                  {diagnosis.results.map((result, index) => (
                    <article key={result.root_cause_id} className="result-card">
                      <div className="result-header">
                        <h3>{index + 1}. {result.cause_name}</h3>
                        <div className="pill-row">
                          <span className="pill">Score {result.score}</span>
                          <span className="pill">{result.confidence}</span>
                        </div>
                      </div>
                      <div className="result-block">
                        <h4>Why it ranked this way</h4>
                        <ul>{result.why.map((line, i) => <li key={i}>{line}</li>)}</ul>
                      </div>
                      <div className="result-block">
                        <h4>Recommended actions</h4>
                        <ul>{result.actions.map((action, i) => <li key={i}>{action}</li>)}</ul>
                      </div>
                    </article>
                  ))}
                </div>
              ) : <p>No root causes scored above zero with the current answers.</p>}
            </section>

            <section className="card">
              <div className="summary-header">
                <h2>Service Summary</h2>
                <button type="button" onClick={copySummary}>Copy Summary</button>
              </div>
              <textarea rows="16" value={serviceSummary} onChange={(e) => setServiceSummary(e.target.value)} />
            </section>

            <section className="card">
              <h2>Feedback Capture</h2>
              <div className="stack">
                <label className="field">
                  <span>Actual Root Cause</span>
                  <input type="text" value={feedbackForm.actual_root_cause} onChange={(e) => setFeedbackForm((prev) => ({ ...prev, actual_root_cause: e.target.value }))} placeholder="What was the real cause?" />
                </label>
                <label className="field">
                  <span>Was the top prediction correct?</span>
                  <select value={feedbackForm.was_prediction_correct} onChange={(e) => setFeedbackForm((prev) => ({ ...prev, was_prediction_correct: e.target.value }))}>
                    <option value="yes">Yes</option><option value="no">No</option>
                  </select>
                </label>
                <label className="field">
                  <span>Technician Notes</span>
                  <textarea rows="4" value={feedbackForm.technician_notes} onChange={(e) => setFeedbackForm((prev) => ({ ...prev, technician_notes: e.target.value }))} />
                </label>
                <button type="button" onClick={handleSaveFeedback} disabled={loading || !feedbackForm.actual_root_cause.trim()}>
                  {loading ? "Saving..." : "Save Feedback"}
                </button>
              </div>
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
}
