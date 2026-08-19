import { useState } from 'react'
import { IB_CHAT_URL } from '../ibStream'

let nextId = 0

// One exchange, one row: the question in its own div on the left, the
// (possibly still-loading) answer in its own div to the right -- the
// literal layout asked for, and stacking exchanges vertically doubles as
// this component's visible conversation history.
function ChatExchange({ exchange }) {
  return (
    <div className="chatbot-exchange">
      <div className="chatbot-question-col">
        <div className="chatbot-col-label">You asked</div>
        <div className="chatbot-question-text">{exchange.question}</div>
      </div>
      <div className="chatbot-answer-col">
        <div className="chatbot-col-label">Answer</div>
        {exchange.loading && <div className="chatbot-thinking">Thinking…</div>}
        {exchange.error && <div className="chatbot-error">{exchange.error}</div>}
        {!exchange.loading && !exchange.error && exchange.answer && (
          <div className="chatbot-answer-text">
            {exchange.answer
              .split('\n')
              .filter((line) => line.trim())
              .map((line, i) => (
                <p key={i}>{line}</p>
              ))}
          </div>
        )}
      </div>
    </div>
  )
}

// Tool-calling chatbot (LangChain + a local Ollama model, see chatbot.py)
// over this project's own data: screener scores/ratings, the
// Recommendations candidate pool, news+sentiment, insider Form 4 activity,
// 13F institutional holdings, theme tags, business descriptions, and live
// positions/prices/account status. ib_server.py's /api/chat keeps no
// server-side session -- prior exchanges are resent as `history` on every
// new request, same "client holds the state, server is stateless per
// request" convention this app already uses everywhere else (no session
// store exists anywhere in this codebase).
export default function RecommendationsChatbot() {
  const [question, setQuestion] = useState('')
  const [exchanges, setExchanges] = useState([])
  const [submitting, setSubmitting] = useState(false)

  function submit(e) {
    e.preventDefault()
    const q = question.trim()
    if (!q || submitting) return

    const id = nextId++
    // Only successfully-answered prior exchanges feed back in as history --
    // a still-loading or errored one has no assistant turn to replay.
    const historyPayload = exchanges
      .filter((ex) => ex.answer && !ex.error)
      .flatMap((ex) => [
        { role: 'user', content: ex.question },
        { role: 'assistant', content: ex.answer },
      ])

    setSubmitting(true)
    setQuestion('')
    setExchanges((prev) => [...prev, { id, question: q, answer: null, loading: true, error: null }])

    fetch(IB_CHAT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, history: historyPayload }),
    })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.json()
      })
      .then((data) => {
        if (data.error) throw new Error(data.error)
        setExchanges((prev) => prev.map((ex) => (ex.id === id ? { ...ex, answer: data.answer, loading: false } : ex)))
      })
      .catch((e) => {
        setExchanges((prev) =>
          prev.map((ex) =>
            ex.id === id
              ? {
                  ...ex,
                  loading: false,
                  error: `Couldn't reach the chatbot — is Ollama running and ib_server.py serving /api/chat? (${e.message})`,
                }
              : ex
          )
        )
      })
      .finally(() => setSubmitting(false))
  }

  function onKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit(e)
    }
  }

  return (
    <div className="asset-card chatbot-panel">
      <h2>Ask about your screener &amp; portfolio</h2>

      {exchanges.length > 0 && (
        <div className="chatbot-history">
          {exchanges.map((ex) => (
            <ChatExchange key={ex.id} exchange={ex} />
          ))}
        </div>
      )}

      <form className="chatbot-form" onSubmit={submit}>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="e.g. Why is REX rated Sell? What's my exposure to gold?"
          rows={2}
        />
        <button type="submit" disabled={submitting || !question.trim()}>
          {submitting ? 'Asking…' : 'Ask'}
        </button>
      </form>
    </div>
  )
}
