/**
 * Client-side tools the browser implements.
 *
 * A client-side tool (`tool_type: "client_side"`) is registered on the agent
 * service as JSON only - name, description, parameter schema - and the service
 * never runs it.  The model calls it, the inference loop ends normally, and the
 * call comes back to us (`client_tool_calls`), so *this* file is where the tool
 * actually happens: for AskUser that is a modal dialog over the conversation.
 *
 * The same contract lives in two other places on purpose, because every front
 * end executes its own client tools:
 *   - `runtime/client_tools.py`  the specification + `tools.json` registration
 *   - `tui/client_tools.py`      the terminal implementation
 * `tests/test_client_tool_catalogue.py` fails if the three drift apart.
 */

export const ASKUSER_TOOL = {
  name: 'AskUser',
  description:
    'Ask the human user a question and wait for them to choose one of the given ' +
    'options. Use it when only the user can decide: a requirement that is ' +
    'ambiguous, a real trade-off between approaches, a risky or irreversible ' +
    'action, or a choice among alternatives you cannot rank from the information ' +
    'given. Ask one question per call, with 2-8 short mutually exclusive options. ' +
    'The result is the chosen option verbatim. Take that choice into account in ' +
    'the next step, and never invent an answer on the user\'s behalf.',
  parameters: {
    type: 'object',
    properties: {
      question: {
        type: 'string',
        description:
          'The question to put in front of the user. Ask it in the language the ' +
          'user is writing in, in one or two sentences, self-contained enough to ' +
          'answer without re-reading the transcript.',
      },
      options: {
        type: 'array',
        minItems: 2,
        maxItems: 8,
        items: { type: 'string', minLength: 1 },
        description:
          "The choices the user can pick from, in display order. They must be " +
          'mutually exclusive and actionable, a few words each; the chosen string ' +
          'comes back verbatim, so phrase each one as the decision it represents. ' +
          "Free text is not offered - if 'something else' is a real possibility, " +
          'say so in the question and let the user answer in their next message.',
      },
      timeout: {
        type: 'integer',
        minimum: 0,
        default: 0,
        description:
          'Seconds to wait for the user. 0 (the default) waits forever, which is ' +
          'the right choice for anything the user has to think about. Use a ' +
          'positive value only when waiting is actually worse than deciding, and ' +
          'pair it with `default_option`.',
      },
      default_option: {
        type: 'string',
        description:
          'The option applied by itself when the countdown in `timeout` runs out ' +
          'and the user has not answered. Must be one of `options`; ignored when ' +
          '`timeout` is 0.',
      },
    },
    required: ['question', 'options'],
  },
}

/** The client-side tools this browser knows how to run. */
const IMPLEMENTED_TOOLS = [ASKUSER_TOOL]

/**
 * The `client_tools` field for one inference request: only the tools the user
 * enabled. Fails closed — no argument means "declare nothing". Declaring them
 * unconditionally was a real mistake: the model popped an AskUser dialog in a
 * conversation where the user had never ticked the tool, and a tool that can
 * block the turn on a dialog is not something to switch on behind their back.
 */
export function clientToolDefinitions(enabledNames = []) {
  const enabled = new Set(enabledNames ?? [])
  return IMPLEMENTED_TOOLS.filter((tool) => enabled.has(tool.name))
}

/** Names this browser can run, whether or not the user has enabled them. */
export function clientToolNames() {
  return IMPLEMENTED_TOOLS.map((tool) => tool.name)
}

/**
 * Of the catalogue rows the user ticked in the tool picker, the client-side
 * ones this browser can run. Ticked but unimplemented names are dropped: the
 * service would hand the call back and we would be the ones who could not
 * answer it.
 */
export function enabledClientToolNames(selectedTools, implemented = clientToolNames()) {
  const known = new Set(implemented)
  return (selectedTools ?? [])
    .filter((tool) => tool && tool.tool_type === 'client_side' && known.has(tool.name))
    .map((tool) => tool.name)
}

/** A tool result saying "this client cannot run that", as the service would. */
export const CANCELLED_ANSWER = '(the user closed the question without choosing)'

/**
 * Parse the `arguments` of a handed-back call: the service hands back a JSON
 * string, streamed tool-call deltas may already have merged into an object, and
 * a model may send numbers as strings.  Tolerant by design - the point is to
 * still show the user a usable question.
 */
export function parseClientToolArguments(args) {
  if (args && typeof args === 'object') return args
  if (typeof args !== 'string' || !args.trim()) return {}
  try {
    const parsed = JSON.parse(args)
    if (parsed && typeof parsed === 'object') return parsed
    return { _raw: parsed }
  } catch {
    return { _raw: args }
  }
}

/** Normalise one AskUser call into `{ question, options, timeout, defaultOption }`. */
export function askUserRequest(args) {
  const parsed = parseClientToolArguments(args)
  const question = String(parsed.question ?? parsed._raw ?? '').trim()
  let rawOptions = parsed.options
  if (typeof rawOptions === 'string') {
    rawOptions = rawOptions.split('\n').map((line) => line.trim()).filter(Boolean)
  }
  const options = []
  for (const item of Array.isArray(rawOptions) ? rawOptions : []) {
    const text = String(item ?? '').trim()
    if (text && !options.includes(text)) options.push(text)
  }
  const timeoutValue = Number.parseInt(parsed.timeout ?? 0, 10)
  const timeout = Number.isFinite(timeoutValue) && timeoutValue > 0 ? timeoutValue : 0
  const defaultOption = String(parsed.default_option ?? '').trim()
  return {
    question: question || '\u8bf7\u9009\u62e9',
    options,
    timeout,
    defaultOption,
  }
}

/**
 * Map whatever the user gave us (exact text, another case, a 1-based number, an
 * unambiguous prefix) onto one of `options`; `null` when nothing matches.
 */
export function normalizeAskUserChoice(options, answer) {
  if (answer === null || answer === undefined) return null
  const text = String(answer).trim()
  if (!text) return null
  if (options.includes(text)) return text
  const folded = text.toLowerCase()
  const insensitive = options.find((option) => option.toLowerCase() === folded)
  if (insensitive) return insensitive
  const index = Number.parseInt(text, 10)
  if (Number.isFinite(index) && String(index) === text && index >= 1 && index <= options.length) {
    return options[index - 1]
  }
  const prefixHits = options.filter((option) => option.toLowerCase().startsWith(folded))
  return prefixHits.length === 1 ? prefixHits[0] : null
}

/**
 * The tool result text for an AskUser answer.
 *
 * An answer that was not really the user's decision says so: otherwise the
 * model reads an auto-selected default as if the user had chosen it.
 */
export function askUserResultText(request, answer, { auto = false, timedOut = false } = {}) {
  const picked = normalizeAskUserChoice(request.options, answer)
  if (picked !== null) {
    if (auto || timedOut) {
      return `${picked} (auto-selected: the user did not answer within ${request.timeout}s)`
    }
    return picked
  }
  if (timedOut || answer === null || answer === undefined) {
    const fallback = normalizeAskUserChoice(request.options, request.defaultOption)
    if (timedOut && fallback !== null) {
      return `${fallback} (auto-selected: the user did not answer within ${request.timeout}s)`
    }
    if (timedOut) {
      return `(no answer: the user did not respond within ${request.timeout}s and no default_option was given)`
    }
    return CANCELLED_ANSWER
  }
  // Free text from a client that allows it: report it instead of dropping it.
  return String(answer).trim()
}

/** The `role: 'tool'` message that answers a handed-back call. */
export function clientToolResultMessage(call, content) {
  const message = { role: 'tool', content: content === null || content === undefined ? '' : String(content) }
  const callId = call?.id || call?.tool_use_id
  if (callId) message.tool_use_id = callId
  if (call?.name) message.name = call.name
  return message
}

/**
 * Client-side tool calls that were declared but never answered.
 *
 * The service names the handoff (`status: client_tool_calls`), and this is the
 * fallback for when it cannot be observed: a page reload, a frame that arrived
 * before the handler was attached, or a service that only declares the call.
 */
export function pickUnansweredClientCalls(messages, names = clientToolNames()) {
  const answered = new Set()
  for (const message of messages || []) {
    if (!message || message.role !== 'tool') continue
    const answeredId = message.tool_use_id || message.tool_call_id
    if (answeredId) answered.add(answeredId)
  }
  const pending = []
  for (const message of messages || []) {
    if (!message || message.role !== 'assistant' || !Array.isArray(message.tool_calls)) continue
    for (const call of message.tool_calls) {
      if (!call) continue
      const name = call.name || call.function?.name
      if (!names.includes(name)) continue
      const callId = call.id || call.tool_use_id
      if (callId && answered.has(callId)) continue
      const args = call.arguments ?? call.function?.arguments
      pending.push({
        id: callId || `client-${name}-${pending.length}`,
        name,
        arguments: typeof args === 'string' ? args : JSON.stringify(args ?? {}),
      })
    }
  }
  return pending
}

/** True when a streamed frame is the service naming its handed-back calls. */
export function clientToolCallFrames(msg) {
  if (!msg || msg.role !== 'status' || msg.status !== 'client_tool_calls') return null
  return Array.isArray(msg.client_tool_calls) ? msg.client_tool_calls : []
}

/**
 * Run one client tool call in the browser.
 *
 * `askUser(request)` shows the dialog and resolves to the chosen option (or
 * `null` for "no answer"); it is injected so this module stays UI-free and unit
 * testable, exactly like the terminal's injected console.
 */
export async function runClientToolCall(call, { askUser } = {}) {
  const args = parseClientToolArguments(call.arguments)
  if (call.name === 'AskUser') {
    const request = askUserRequest(args)
    if (!request.options.length) return CANCELLED_ANSWER
    const outcome = await askUser(request)
    const answer = outcome && typeof outcome === 'object' ? outcome.value : outcome
    const timedOut = Boolean(outcome && typeof outcome === 'object' && outcome.timedOut)
    const auto = Boolean(outcome && typeof outcome === 'object' && outcome.auto)
    return askUserResultText(request, answer, { auto, timedOut })
  }
  return JSON.stringify({
    error: 'UnknownClientTool',
    message: `this browser cannot execute '${call.name}'`,
  })
}
