import { describe, expect, it } from 'vitest'
import {
  ASKUSER_TOOL,
  CANCELLED_ANSWER,
  askUserRequest,
  askUserResultText,
  clientToolCallFrames,
  clientToolDefinitions,
  clientToolNames,
  createQuestionLedger,
  clientToolResultMessage,
  enabledClientToolNames,
  normalizeAskUserChoice,
  parseClientToolArguments,
  pickUnansweredClientCalls,
  runClientToolCall,
} from './client-tools.js'

describe('client tool definitions', () => {
  it('ships AskUser and nothing else', () => {
    expect(clientToolNames()).toEqual(['AskUser'])
    expect(clientToolDefinitions(['AskUser'])[0].name).toBe('AskUser')
    expect(ASKUSER_TOOL.parameters.type).toBe('object')
    expect(ASKUSER_TOOL.parameters.required).toEqual(['question', 'options'])
    // 0 is "wait forever", not "unset": the model must not have to guess.
    expect(ASKUSER_TOOL.parameters.properties.timeout.default).toBe(0)
  })

  describe('what a request may declare', () => {
    // The bug this guards: AskUser was declared on every request, so a model
    // could raise a question dialog in a conversation where the user had never
    // enabled the tool.
    it('declares nothing unless the user enabled the tool', () => {
      expect(clientToolDefinitions()).toEqual([])
      expect(clientToolDefinitions([])).toEqual([])
      expect(clientToolDefinitions(['exec_shell', 'local_shell'])).toEqual([])
    })

    it('declares AskUser once it is enabled', () => {
      expect(clientToolDefinitions(['AskUser'])).toEqual([
        {
          tool_id: ASKUSER_TOOL.tool_id,
          name: 'AskUser',
          description: ASKUSER_TOOL.description,
          parameters: ASKUSER_TOOL.parameters,
        },
      ])
    })

    it('picks the enabled client tools out of the ticked catalogue rows', () => {
      const ticked = [
        { tool_id: 'client_side-AskUser', name: 'AskUser', tool_type: 'client_side' },
        { tool_id: 'function-exec_shell', name: 'exec_shell', tool_type: 'function' },
        { tool_id: 'skill-docx', name: 'docx', tool_type: 'skill' },
      ]
      expect(enabledClientToolNames(ticked)).toEqual(['AskUser'])
    })

    it('drops a ticked client tool this browser cannot run', () => {
      // Ticking someone else's client tool in the catalogue must not make us
      // promise to answer it.
      const ticked = [
        { tool_id: 'client_side-AskUser', name: 'AskUser', tool_type: 'client_side' },
        { tool_id: 'client_side-my_tool', name: 'my_tool', tool_type: 'client_side' },
      ]
      expect(enabledClientToolNames(ticked)).toEqual(['AskUser'])
      expect(clientToolDefinitions(enabledClientToolNames(ticked))).toHaveLength(1)
    })

    it('survives an empty or missing selection', () => {
      expect(enabledClientToolNames([])).toEqual([])
      expect(enabledClientToolNames(undefined)).toEqual([])
      // A stale id that is no longer in the catalogue resolves to nothing.
      expect(enabledClientToolNames([undefined])).toEqual([])
    })
  })

  it('is a client-side promise only - nothing about executing it', () => {
    const body = { tool_type: 'client_side', ...ASKUSER_TOOL }
    expect(body).not.toHaveProperty('function_file_path')
    expect(body).not.toHaveProperty('function_name')
  })
})

describe('parseClientToolArguments', () => {
  it('accepts the JSON string the service hands back', () => {
    expect(parseClientToolArguments('{"question":"部署方式？","options":["蓝绿","金丝雀"]}')).toEqual({
      question: '部署方式？',
      options: ['蓝绿', '金丝雀'],
    })
  })

  it('accepts an object already merged from stream deltas', () => {
    expect(parseClientToolArguments({ timeout: 30 })).toEqual({ timeout: 30 })
  })

  it('keeps unparsable arguments instead of losing the question', () => {
    expect(parseClientToolArguments('要不要发布')).toEqual({ _raw: '要不要发布' })
    expect(parseClientToolArguments('')).toEqual({})
    expect(parseClientToolArguments(null)).toEqual({})
  })
})

describe('askUserRequest', () => {
  it('reads the declared fields', () => {
    const request = askUserRequest('{"question":"用哪个方案?","options":["A","B"],"timeout":20,"default_option":"B"}')
    expect(request).toEqual({ question: '用哪个方案?', options: ['A', 'B'], timeout: 20, defaultOption: 'B' })
  })

  it('treats timeout 0 as no timeout', () => {
    expect(askUserRequest({ question: 'q', options: ['a', 'b'] }).timeout).toBe(0)
    expect(askUserRequest({ question: 'q', options: ['a', 'b'], timeout: -5 }).timeout).toBe(0)
    expect(askUserRequest({ question: 'q', options: ['a', 'b'], timeout: '15' }).timeout).toBe(15)
  })

  it('tolerates a model that sends options as one string', () => {
    expect(askUserRequest({ question: 'q', options: ' 蓝绿 \n 金丝雀 \n\n 蓝绿 ' }).options).toEqual(['蓝绿', '金丝雀'])
  })

  it('falls back to the raw text as the question', () => {
    expect(askUserRequest('先发布吗?').question).toBe('先发布吗?')
    expect(askUserRequest({}).question).toBeTruthy()
  })

  it('drops blank and duplicate options', () => {
    expect(askUserRequest({ question: 'q', options: ['a', ' a ', '', 'b'] }).options).toEqual(['a', 'b'])
  })
})

describe('normalizeAskUserChoice', () => {
  const options = ['蓝绿发布', '金丝雀发布', '直接替换']

  it('accepts the exact option', () => {
    expect(normalizeAskUserChoice(options, '金丝雀发布')).toBe('金丝雀发布')
  })

  it('accepts another case or extra whitespace', () => {
    expect(normalizeAskUserChoice(['Yes', 'No'], '  yes ')).toBe('Yes')
  })

  it('accepts a 1-based number', () => {
    expect(normalizeAskUserChoice(options, '2')).toBe('金丝雀发布')
    expect(normalizeAskUserChoice(options, '0')).toBeNull()
    expect(normalizeAskUserChoice(options, '4')).toBeNull()
  })

  it('accepts an unambiguous prefix only', () => {
    expect(normalizeAskUserChoice(options, '直接')).toBe('直接替换')
    expect(normalizeAskUserChoice(options, '发')).toBeNull()
  })

  it('reports no answer for nothing', () => {
    expect(normalizeAskUserChoice(options, null)).toBeNull()
    expect(normalizeAskUserChoice(options, '')).toBeNull()
  })
})

describe('askUserResultText', () => {
  const request = { question: 'q', options: ['蓝绿发布', '金丝雀发布'], timeout: 0, defaultOption: '' }

  it('returns the chosen option verbatim', () => {
    expect(askUserResultText(request, '金丝雀发布')).toBe('金丝雀发布')
    expect(askUserResultText(request, '金丝雀')).toBe('金丝雀发布')
  })

  it('says out loud when the answer was not the user\'s decision', () => {
    const withDefault = { ...request, timeout: 10, defaultOption: '蓝绿发布' }
    expect(askUserResultText(withDefault, null, { timedOut: true, auto: true }))
      .toBe('蓝绿发布 (auto-selected: the user did not answer within 10s)')
  })

  it('reports no answer when there is no default to fall back on', () => {
    expect(askUserResultText({ ...request, timeout: 10 }, null, { timedOut: true }))
      .toBe('(no answer: the user did not respond within 10s and no default_option was given)')
  })

  it('reports a dismissed question honestly', () => {
    expect(askUserResultText(request, null)).toBe(CANCELLED_ANSWER)
  })

  it('keeps free text a client offered instead of dropping it', () => {
    expect(askUserResultText(request, '先等我把测试跑完')).toBe('先等我把测试跑完')
  })
})

describe('handoff plumbing', () => {
  it('recognises the frame that names the handed-back calls', () => {
    expect(clientToolCallFrames({ role: 'status', status: 'client_tool_calls', client_tool_calls: [{ id: 'c1', name: 'AskUser' }] }))
      .toEqual([{ id: 'c1', name: 'AskUser' }])
    expect(clientToolCallFrames({ role: 'assistant', content: 'hi' })).toBeNull()
    expect(clientToolCallFrames({ role: 'status', status: 'streaming' })).toBeNull()
  })

  it('finds an unanswered call in the history when no frame was observed', () => {
    const messages = [
      { role: 'user', content: '部署吧' },
      {
        role: 'assistant',
        tool_calls: [
          { id: 'c1', name: 'AskUser', arguments: '{"question":"q","options":["a","b"]}' },
          { id: 'c9', name: 'exec_shell', arguments: '{}' },
        ],
      },
      { role: 'tool', tool_use_id: 'c9', content: 'ok' },
    ]
    expect(pickUnansweredClientCalls(messages)).toEqual([
      { id: 'c1', name: 'AskUser', arguments: '{"question":"q","options":["a","b"]}' },
    ])
    // answered -> nothing pending
    expect(pickUnansweredClientCalls([...messages, { role: 'tool', tool_use_id: 'c1', content: 'b' }])).toEqual([])
    // the OpenAI-shaped arguments of a stored call
    const openai = [{ role: 'assistant', tool_calls: [{ id: 'c2', function: { name: 'AskUser', arguments: '{"question":"q"}' } }] }]
    expect(pickUnansweredClientCalls(openai)[0].arguments).toBe('{"question":"q"}')
  })

  it('answers with the message shape the service expects', () => {
    expect(clientToolResultMessage({ id: 'c1', name: 'AskUser' }, '金丝雀发布')).toEqual({
      role: 'tool',
      content: '金丝雀发布',
      tool_use_id: 'c1',
      name: 'AskUser',
    })
  })
})

describe('runClientToolCall', () => {
  it('asks through the injected dialog and returns the chosen option', async () => {
    const asked = []
    const content = await runClientToolCall(
      { id: 'c1', name: 'AskUser', arguments: '{"question":"部署方式？","options":["蓝绿","金丝雀"]}' },
      { askUser: async (request) => { asked.push(request); return { value: '金丝雀' } } },
    )
    expect(asked[0]).toEqual({ question: '部署方式？', options: ['蓝绿', '金丝雀'], timeout: 0, defaultOption: '' })
    expect(content).toBe('金丝雀')
  })

  it('labels an auto-selected default in the result', async () => {
    const content = await runClientToolCall(
      { name: 'AskUser', arguments: { question: 'q', options: ['a', 'b'], timeout: 5, default_option: 'a' } },
      { askUser: async () => ({ value: 'a', auto: true, timedOut: true }) },
    )
    expect(content).toBe('a (auto-selected: the user did not answer within 5s)')
  })

  it('says so when the user closed the question', async () => {
    const content = await runClientToolCall(
      { name: 'AskUser', arguments: '{"question":"q","options":["a","b"]}' },
      { askUser: async () => ({ value: null, dismissed: true }) },
    )
    expect(content).toBe(CANCELLED_ANSWER)
  })

  it('never invents an answer for a question with no options', async () => {
    let called = false
    const content = await runClientToolCall(
      { name: 'AskUser', arguments: '{"question":"q"}' },
      { askUser: async () => { called = true; return { value: 'a' } } },
    )
    expect(called).toBe(false)
    expect(content).toBe(CANCELLED_ANSWER)
  })

  it('refuses a client tool this browser does not implement', async () => {
    const content = await runClientToolCall({ name: 'local_shell', arguments: '{"command":"ls"}' }, {})
    const parsed = JSON.parse(content)
    expect(parsed.error).toBe('UnknownClientTool')
    expect(parsed.message).toContain('local_shell')
  })
})

describe('the question ledger', () => {
  // Why this exists: the page used to keep one dialog slot. A question was
  // thrown away whenever anything else happened - switching sessions, a second
  // handoff, a question from another conversation - and the discarded question
  // resolved as "the user did not choose", so the model kept reasoning on an
  // answer the user never had the chance to see. These are those regressions.
  const req = (text) => ({ question: text, options: ['a', 'b'], timeout: 0, defaultOption: '' })

  it('keeps a question with the session that asked it', async () => {
    const ledger = createQuestionLedger()
    const handle = ledger.ask('s1', 'call_1', req('部署方式？'))
    expect(ledger.visibleFor('s1').request.question).toBe('部署方式？')
    expect(ledger.visibleFor('s2')).toBe(null)
    expect(ledger.pendingSessionIds()).toEqual(['s1'])
    ledger.settle(handle.key, { value: 'a' })
    await expect(handle.promise).resolves.toEqual({ value: 'a' })
    expect(ledger.items).toEqual([])
  })

  it('does not let another conversation steal the question on screen', async () => {
    const ledger = createQuestionLedger()
    const first = ledger.ask('s1', 'call_1', req('第一个问题'))
    const second = ledger.ask('s2', 'call_2', req('第二个问题'))
    // Both stay pending; each session sees exactly its own question.
    expect(ledger.visibleFor('s1').call_id).toBe('call_1')
    expect(ledger.visibleFor('s2').call_id).toBe('call_2')
    expect(ledger.pendingSessionIds()).toEqual(['s1', 's2'])
    ledger.settle(ledger.visibleFor('s2').key, { value: 'b' })
    // Settling one must not resolve the other: the first user's turn waits.
    expect(ledger.visibleFor('s1').call_id).toBe('call_1')
    ledger.settle(first.key, { value: 'a' })
    await expect(first.promise).resolves.toEqual({ value: 'a' })
    await expect(second.promise).resolves.toEqual({ value: 'b' })
  })

  it('shows the oldest question first when one session asks twice', () => {
    const ledger = createQuestionLedger()
    ledger.ask('s1', 'call_1', req('先问的'))
    ledger.ask('s1', 'call_2', req('后问的'))
    expect(ledger.visibleFor('s1').call_id).toBe('call_1')
    ledger.settle(ledger.visibleFor('s1').key, { value: 'a' })
    expect(ledger.visibleFor('s1').call_id).toBe('call_2')
  })

  it('survives switching away and back', () => {
    const ledger = createQuestionLedger()
    ledger.ask('s1', 'call_1', req('切走了还在吗'))
    expect(ledger.visibleFor('s2')).toBe(null)   // 切到别的会话：这里没有它
    expect(ledger.visibleFor('s1').call_id).toBe('call_1')  // 切回来：还在
    expect(ledger.pendingSessionIds()).toEqual(['s1'])
  })

  it('re-opening a session must not ask the same call twice', () => {
    const ledger = createQuestionLedger()
    ledger.ask('s1', 'call_1', req('已经在等了'))
    expect(ledger.isQueued('s1', 'call_1')).toBe(true)
    expect(ledger.isQueued('s2', 'call_1')).toBe(false)
    expect(ledger.isQueued('s1', 'call_9')).toBe(false)
  })

  it('ends a stopped turn as abandoned, without touching other sessions', async () => {
    const ledger = createQuestionLedger()
    const mine = ledger.ask('s1', 'call_1', req('这一轮被停掉了'))
    const other = ledger.ask('s2', 'call_2', req('别的情况不受影响'))
    expect(ledger.dropSession('s1')).toBe(1)
    expect(mine.abandoned).toBe(true)
    await expect(mine.promise).resolves.toEqual({ value: null, dismissed: true, abandoned: true })
    expect(ledger.visibleFor('s2').call_id).toBe('call_2')
    expect(ledger.dropSession('s1')).toBe(0)
    ledger.settle(other.key, { value: 'a' })
  })

  it('ignores settling the same question twice', async () => {
    const ledger = createQuestionLedger()
    const handle = ledger.ask('s1', 'call_1', req('双击也不会重复送答案'))
    expect(ledger.settle(handle.key, { value: 'a' })).toBe(true)
    expect(ledger.settle(handle.key, { value: 'b' })).toBe(false)
    await expect(handle.promise).resolves.toEqual({ value: 'a' })
  })

  it('drops a question another client of the session answered', async () => {
    // The result frame of a client-side call is brokered to every client of the
    // session, so a browser that did not run the tool sees it being answered.
    // Its own question is stale from that moment: the decision is already made,
    // and answering would put a second result on a call that has exactly one.
    const ledger = createQuestionLedger()
    const answered = ledger.ask('s1', 'call_1', req('别人已经答过了'))
    const waiting = ledger.ask('s1', 'call_2', req('这个还没人答'))
    expect(ledger.settleExternallyAnswered('s1', 'call_1')).toBe(1)
    // abandoned: the caller must not post a result for it
    expect(answered.abandoned).toBe(true)
    await expect(answered.promise).resolves.toEqual({
      value: null, dismissed: true, abandoned: true,
    })
    // Only that call was settled - the other question of the same session waits.
    expect(ledger.visibleFor('s1').call_id).toBe('call_2')
    // A result frame nobody here is waiting for changes nothing.
    expect(ledger.settleExternallyAnswered('s1', 'call_server_side')).toBe(0)
    expect(ledger.settleExternallyAnswered(null, 'call_2')).toBe(0)
    ledger.settle(waiting.key, { value: 'a' })
  })
})
