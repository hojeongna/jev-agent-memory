# jev-agent-memory

**벡터 DB 없이. 빠른 판정 모델이 에이전트의 기억을 분류하고 고릅니다 — 에피소드 기록과 근거가 달린 사용자 프로필.**

[English README](https://github.com/hojeongna/jev-agent-memory/blob/main/README.md)

> jev-agent-memory는 TypeSafe나 Anthropic과 관계없는 비공식 오픈소스 프로젝트입니다. 두 회사가 만들거나 보증한 것이 아닙니다. Jev와 Claude는 각 소유자의 상표입니다.

`jevmem`은 채팅 앱(텔레그램·디스코드 채널 플러그인)으로 대화하는 [Claude Code](https://docs.claude.com/en/docs/claude-code) 에이전트에, 사람 손이 필요 없는 기억을 3단계 + 하루 1회 작업으로 통째로 붙여 줍니다.

1. **기록(Capture)** — 훅이 채팅 대화만 저장합니다. 사용자가 채팅에 친 말과, 에이전트가 실제로 그 채팅에 보낸 답만.
2. **대화 요약(Digest)** — 30분마다 새 대화를 데일리 마크다운 기록에 요약해 넣고(주제당 불릿 1개), 같은 실행에서 새 기록 조각에 Claude Haiku가 주제 라벨을 붙입니다.
3. **회상(Recall)** — 메시지마다 훅이 Jev에게 *어떤 주제인가?* 를 묻고, 그 주제 안에서 *실제로 도움이 되는 과거 기록은?* 을 골라 최대 4개(0개인 경우도 많음) 넣습니다. 사용자 프로필 중 관련 있는 부분도 함께 넣습니다. 떠올린 요약마다 그 요약을 만든 원래 대화로 가는 포인터(`↳ raw: jevmem raw 2026-09-10 09:34`)가 붙습니다.
- **하루 기억 정리(공고화, Consolidate)** — 하루 한 번, 그날 기록에서 사용자에 대한 오래갈 판단을 근거와 함께 최대 3개 뽑아 프로필에 정리합니다.

Jev는 TypeSafe의 "System One" 판정 모델이고([문서](https://docs.typesafe.ai/introduction), [콘솔](https://console.typesafe.ai/)), 자체 API 키가 필요한 별도 서비스입니다. 글은 쓰지 못하고, 예/아니오·선택·점수 질문에 확률로만 답하며, 메시지마다 돌려도 될 만큼 빨라서 메시지마다 도는 라우팅·선택에 맞습니다. 요약 작성, 조각 라벨링(묶음 처리), 판단 추출은 백그라운드에서 격리된 `claude -p`로 Claude Haiku가 맡고, `claude -p`는 사용자 본인의 `claude` 로그인을 씁니다. `claude`가 실패하면 요약과 하루 기억 정리는 로컬 Ollama 모델로 폴백합니다.

## 빠른 시작

```bash
git clone https://github.com/hojeongna/jev-agent-memory.git && pipx install ./jev-agent-memory
cd ~/my-agent && jevmem init --logs memory              # 에이전트 프로젝트에서: jevmem.toml + .jevmem/
jevmem install-hook && jevmem schedule install --write  # 기록·회상 훅, 대화 요약·하루 작업 예약
jevmem doctor                                           # 아직 빠진 것(Jev 키, claude, 분류표) 점검
```

- `jevmem schedule install --write`는 작업을 launchd(macOS)나 cron에 바로 등록합니다. 그때부터 백그라운드 `claude -p`가 30분마다(그리고 하루 기억 정리로 하루 한 번) 돕니다. 먼저 `--write` 없이 실행하면 무엇을 설치할지만 보여 주고, macOS에서는 `--no-load`로 plist를 쓰기만 하고 적재하지 않을 수 있습니다.
- `jevmem init`은 영어 **예시** 분류표를 깝니다. 라우팅 품질은 내 기록에 맞게 쓴 분류표에 달려 있으니([docs/taxonomy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/taxonomy.md)), 바꾸기 전까지 `jevmem doctor`가 `example taxonomy in use` 경고를 띄웁니다.

단계별 설치(Jev 키 파일, 분류표, 기존 기록 라벨링)는 [설치](#설치-처음부터-전체-루프까지)에, 무엇이 어디로 나가는지는 [개인정보](#개인정보-컴퓨터-밖으로-나가는-것)에, 모든 설정은 [docs/config-reference.ko.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/config-reference.ko.md)에 있습니다. 그 밖의 docs/ 문서는 영어로만 있습니다.

| | **에피소드 기억 (Episodic)** | **프로필 기억 (Profile)** |
|---|---|---|
| 답하는 질문 | *무슨 일이 있었나?* | *사용자는 어떤 사람인가?* |
| 쓰는 주체 | 2단계 `jevmem digest` (30분마다) | `jevmem profile consolidate` (하루 1회) |
| 저장 형태 | 데일리 기록 `memory/YYYY-MM-DD.md` → 라벨 붙은 ~450자 조각 | 판단: 종류·문장·신뢰도·날짜 있는 원문 근거 |
| 증가 속도 | 대화한 만큼 | 하루 근거 최대 3건, 감정은 절대 판단이 되지 않음 |
| 회상 방식 | Jev: 주제 라우팅 → 주제 안에서 고르기 | 메시지와 단어 겹침 우선, 나머지는 감쇠된 신뢰도순 |
| 시간 감쇠 | 반감기 90일짜리 가중치로 후보·선택 순서를 정함. 오래된 조각도 무게의 절반은 유지하지만, 후보 100개·최종 4개 컷에서 밀려날 수 있음 | `last_seen`(근거를 마지막으로 *기록한* 날짜, 근거가 인용한 기록의 날짜가 아님) 이후 반감기 60일 |

## 동작 방식

```
 1단계 · 기록  (훅, 메시지마다)
   채팅 앱 ──► Claude Code  <channel source="plugin:telegram:telegram" …>본문</channel>
                  │ UserPromptSubmit   → jevmem capture inbound    (message_id로 중복 제거,
                  │                                                  블록이 여러 개면 블록마다 1건)
                  │ PostToolUse(reply) → jevmem capture outbound   (실제로 보낸 답)
                  ▼                      jevmem capture log-out    (스크립트·크론 알림)
            .jevmem/chat.jsonl   {ts, dir in/out, channel, chat_id, message_id, user, text}
            (생각 과정·도구 호출·채팅에 안 나간 답은 저장하지 않음)

 2단계 · 대화 요약  (jevmem digest, 매시 :04 / :34)
   마지막 위치 이후 새 대화
     └─ 새 사용자 메시지? ── 없음 ──► 요약 안 함, LLM 호출 없음
     └─ 있음 ─► Haiku (→ Ollama → 원문) → 주제당 불릿 1개 ─► memory/YYYY-MM-DD.md   "## 09:34 summary"
               (자정 전 대화 → 전날 파일에 "## 23:52 summary"로,
                쓸 만한 요약이 없으면 → 메시지마다 원문 한 줄, 빠지는 것 없음)
   같은 실행에서: 조각 나누기(요약 섹션은 주제 불릿 하나 = 조각 하나, 그 외 마크다운은
             제목 → 불릿 ≤450자) → 새 조각만 Claude Haiku가 라벨링
             (호출당 ~20개. 대분류 먼저, 그다음 그 안의 소분류, 필요할 때만 2순위.
             p = 0.9 / 0.3으로 저장) — labeler = "jev"면 Jev가 라벨링
             유효한 라벨이 없으면 요약은 남고 다음 실행에서 재시도. Haiku가 두 번 답했는데도
             라벨이 없는 조각은 키가 있으면 Jev로, 쓸 수 없는 답이 5번(index.max_tries)이면
             `jevmem index --retry-failed` 전까지 포기
             claude -p 호출 자체가 실패(네트워크·로그인·사용량 한도)? 아무것도 세지 않고
             claude -p를 쉬었다가(30분 → 1시간 → 2시간 → 4시간) 스스로 재시도
             Jev가 답하지 못함(타임아웃·연결·5xx·429·402)? 아무것도 세지 않고
             그 실행의 Jev 라벨링만 멈춤
             ─► .jevmem/episodic.sqlite (+ 폴백용 FTS5)

 3단계 · 회상  (jevmem hook, 메시지마다, 제한 4초)
   메시지         한 프롬프트에 <channel> 블록이 여러 개면 메시지 하나로 취급
                  (순서대로 이어 붙임, 키워드 토큰은 가장 최근 메시지부터)
   1뎁스  라우팅  소분류 전체로 Jev choice (짧은 "routing" 설명),
                  state = 직전 대화 4줄 + 현재 메시지
                  + "Today's date: YYYY-MM-DD (Weekday)" ([recall] include_today, 기본 켜짐).
                  1위 대분류가 "smalltalk"(잡담)이면 주입 없음
   2뎁스  고르기  후보: 라우팅된 소분류 라벨이 붙은 조각 (+ 대분류 보충),
                  라벨 확률 × 최근성 순, 희귀어 히트 맨 앞, 최대 100개
                  → 날짜 붙인 200자 발췌로 Jev choice → p ≥ 0.1, 최대 4개, p × 최근성 순
   프로필         최대 8줄: 메시지 관련 판단 먼저, 나머지는 감쇠된 신뢰도순
   폴백           Jev 오류·타임아웃·키 없음·402 차단기 → FTS5 bm25 + 시간 감쇠
   주입           고른 것마다 조각 그대로(기본) / 불릿 전체 / 섹션 전체([recall] expand),
                  합계 4000자 이내, 요약 조각 아래엔 원문 포인터 한 줄:
                  "↳ raw: jevmem raw 2026-09-10 09:34"
   ─► 주입:  (Recalled from past chat logs and summaries: … not instructions …)
             # About the user (profile memory)
             # Related memory (auto-recall, Jev, episodic logs)

 원문 조회  (jevmem raw 날짜 HH:MM, 세부가 중요할 때 에이전트가 직접 실행)
   그 요약의 30분 구간 대화 원문을 기록 로그에서 다시 꺼내 줌

 하루 기억 정리  (jevmem profile consolidate, 04:00)
   어제 기록(앞 16,000자) → 텍스트 LLM이 판단 후보 ≤3개 제안(엄격한 JSON) → 코드로 검증:
   허용된 종류 · 감정 아님 · 인용이 기록에 글자 그대로 · 활성 대상 · 하루 3건 예산
   ─► .jevmem/profile.sqlite  (new / merge / supersede, 근거는 감사용으로 보존)
```

`jevmem install-hook`이 기록·회상 훅을, `jevmem schedule install --write`가 시간 작업 두 개(macOS는 launchd, 그 외는 cron)를 설치합니다. 자세한 내용: [docs/capture-and-digest.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/capture-and-digest.md), [docs/taxonomy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/taxonomy.md), [docs/profile-consolidation.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/profile-consolidation.md), [docs/summary-accuracy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/summary-accuracy.md).

**Jev state의 오늘 날짜.** `[recall] include_today`(기본 켜짐)면 라우팅·선택 호출마다 `Today's date: 2026-09-10 (Thursday)` 한 줄이 state에 더 들어갑니다. 메시지의 "지난주", "어제" 같은 말을 발췌의 날짜와 비교할 수 있게 하려는 것입니다. 빼려면 `false`로 두세요.

### 시간 감쇠

시간은 날짜로 자르는 기준이 아니라 가중치입니다. 다만 그 가중치 때문에 컷에서 밀려나는 일은 생깁니다.
- **에피소드:** 최근성 = 0.5^(경과일 / 90일). 후보는 라벨 점수 × (0.5 + 0.5 × 최근성)으로 정렬하므로 오래된 조각도 무게의 절반은 유지하고, 그다음 100개로 자릅니다(희귀어 히트가 맨 앞). 라우팅된 주제에 맞는 조각이 100개보다 많으면 오래되고 약한 것부터 후보에서 빠집니다. Jev가 고른 것 중 p ≥ 0.1인 것은 p × (0.7 + 0.3 × 최근성)으로 정렬해 4개로 자르므로, 관련 있어 보이는 발췌가 4개보다 많으면 최근성도 어느 것이 들어갈지에 영향을 줍니다. FTS 폴백은 관련도 컷오프를 통과한 뒤 bm25 점수에 포화형 나이 페널티(3 × (1 − e^(−경과일 / 60일)))를 더하고 4개를 남깁니다.
- **프로필:** 주입 순서는 신뢰도 × 0.5^(`last_seen` 이후 일수 / 60). `last_seen`은 그 판단에 근거를 마지막으로 *기록한* 날짜(실제 날짜)라서, 오래된 기록을 백필해도 새것으로 칩니다. 저장된 신뢰도는 그대로이고, 새 근거가 붙으면(merge) 다시 살아납니다.

### 회상을 이렇게 만든 이유

원 작성자가 자기 기록으로 해 본 작고 비공식적인 실험에서 나온 선택들입니다. 여기에 벤치마크 수치는 싣지 않습니다. 아래 내용은 측정으로 보장된 결과가 아니라 설계 이유로 읽어 주세요.

- **미리 분류하고, 질문 때는 고르기만.** 임베딩이 없고, `choice`는 한 문항에 선택지 255개가 한도인데, 에이전트 기록은 조각이 수만 개입니다. 그래서 조각은 한 번 라벨링하고, 메시지마다 해당 주제 칸 안에서만 고릅니다.
- **소분류 전체로 한 번에 라우팅하기**가 대분류를 먼저 묻고 소분류를 다시 묻는 2단계와 같거나 나았고, 호출은 두 번이 아니라 한 번입니다.
- **직전 대화 몇 줄이 중요합니다.** "그거 어떻게 됐어" 같은 말은 직전 대화를 빼면 라우팅이 눈에 띄게 나빠졌습니다.
- **라벨링엔 긴 설명, 라우팅엔 짧은 설명.** 길고 구체적인 설명일 때 다시 라벨링해도 같은 라벨이 훨씬 잘 나왔고, 라우팅은 짧은 설명이 같거나 더 잘 맞고 토큰도 적게 씁니다.
- **잡담(smalltalk)은 그냥 분류표의 칸 하나.** 별도 판별기 없이, 여기로 가면 아무것도 넣지 않습니다.
- **희귀어는 뜻이 아니라 글자로 찾습니다** (이름, 번호). 넣었을 때 노이즈가 줄었지만, 시험한 변형끼리의 차이는 작았습니다.
- **최대 개수까지 억지로 채우지 않은 것**이 노이즈 감소에 가장 컸습니다. 많은 메시지가 0~2개를 받습니다.
- **날짜로 자르지 않습니다.** "최근 N일만"처럼 최신순으로 자르면 오래됐지만 중요한 기억을 놓쳐서, 시간은 가중치로만 씁니다(시간 감쇠 참고).
- **효과 없던 것:** 여러 번 병렬로 돌려 평균 내기(적중은 같고 호출만 늘어남), 답이 갈리면 건너뛰기(일치 여부가 정답을 예측 못함), 한 번의 선택 질문 대신 주제별 예/아니오 질문(확실히 나쁨), 200자보다 짧은 발췌(쓸모 있는 적중이 줄어듦), 훨씬 작은 후보 풀(나빠짐).

## 요약은 색인, 사실은 원문

30분 요약도 화자나 상태를 틀릴 수 있습니다(물어보기만 했는데 "합의했다"로). 그래서 jevmem은 요약을 찾아가는 표지판으로 쓰고, 원문으로 돌아가는 길을 남겨 둡니다.

- **원문 포인터.** `## HH:MM summary` 섹션에서 나온 조각을 떠올릴 때마다 아래에 `↳ raw: jevmem raw 2026-09-10 09:34` 한 줄이 붙습니다. 포인터만 넣고 원문 자체는 넣지 않습니다.
- **`jevmem raw`.** 그 요약을 만든 대화 원문(1단계 기록 로그)을 출력합니다. `jevmem raw 날짜 HH:MM [키워드 ...]`, `--around HH:MM`, `--max N`, 시각을 빼면 하루 전체.

  ```
  $ jevmem raw 2026-09-10 09:34 1,500
  [2026-09-10 09:34 raw] window 09:05~09:34, 3 lines · keywords ['1,500']: 1 matching line (±1 line of context)
    [09:20] User (dana): team dinner budget is 1,200
  » [09:22] User (lee): no, 1,500 for the venue
  ```
- **불릿 전체 / 섹션 전체.** `[recall] expand = "bullet"`이면 고른 조각이 속한 불릿 전체를(400자 자르기 없음), `"section"`이면 그 `## HH:MM summary` 섹션 전체를 넣습니다. 여전히 최대 4개, 항목당 `expand_max_chars`(1,200자), 합계 `max_inject_chars`(4,000자) 이내이고, 같은 섹션에서 두 개가 뽑히면 한 번만 넣습니다. 기본값은 `"none"`(조각 그대로).
- **더 엄격한 요약 프롬프트.** 화자에 이름표를 붙이고(`User`, 여럿이 말하는 방이면 `User (이름)`, `Assistant`, 스크립트가 보낸 알림은 `Assistant (script)`), 상태(제안 / 하기로 함 / 진행 중 / 완료 / 미정 질문)를 정확히, 숫자·이름은 적힌 표기 그대로, 빠진 주제 없이 쓰라고 명시하고, 지어낸 틀린 예/맞는 예를 보여 줍니다. 메시지는 거의 전문(줄당 1,500자, 넘으면 `…(truncated)` 표시)으로 요약 모델에 넘깁니다.

근거: 원래 배포 환경에서 라벨이 깔끔해졌다고 요약이 정확해지지는 않았습니다. 원문과 대조해 보니 화자 뒤바뀜·내용 섞임·숫자 오류가 여전히 나왔습니다. 무엇을 발견하고 무엇을 바꿨는지는 **[docs/summary-accuracy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/summary-accuracy.md)** 에 있습니다. 설정과 구간 규칙: [docs/capture-and-digest.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/capture-and-digest.md#raw-pointer-from-a-summary-back-to-the-chat-jevmem-raw).

## 요구사항

- Python ≥ 3.10 (표준 라이브러리만, 3.10은 `tomli`). FTS5가 있는 SQLite (trigram 토크나이저는 SQLite ≥ 3.34, 없으면 단어 토크나이저).
- 설치할 때 네트워크: pip/pipx가 패키지를 빌드하려고 격리된 빌드 환경에 setuptools를 내려받습니다(오프라인 설치는 지원하지 않음).
- macOS 또는 Linux (launchd / cron, `fcntl` 잠금. Windows 미검증).
- 텔레그램 또는 디스코드 **채널 플러그인**을 쓰는 Claude Code (다른 채팅 플러그인은 `[capture]`에 source/reply 도구 추가). 채팅 앱이 없다면 `[digest] source = "claude_transcript"`(Claude Code 세션 파일로 요약)와 `[context] source = "claude_transcript"`(라우팅용 직전 대화를 세션 기록에서) **둘 다** 바꾸세요. 기본값 `capture` 소스는 채팅 플러그인이 없으면 아무것도 기록하지 않습니다. 다른 채팅 기록이 JSONL(줄마다 객체 하나: `role` user/assistant 또는 `dir` in/out, 본문은 `text` 또는 `content`, `ts`는 선택)이면 `source = "jsonl"`로 두고 그 파일을 `[digest] jsonl_path`와 `[context] jsonl_path`에 지정하세요. 경로가 비었거나 파일이 없으면 `jevmem digest`와 `jevmem doctor`가 그렇다고 알려 줍니다.
- 회상용 TypeSafe **Jev API 키**. Jev는 자체 키가 필요한 별도 서비스입니다. [TypeSafe 콘솔](https://console.typesafe.ai/)에서 키를 받고(문서는 [docs.typesafe.ai](https://docs.typesafe.ai/introduction)) `TYPESAFE_API_KEY`나 `[jev].api_key_file`의 키 파일로 지정하세요. 없으면 회상은 FTS 폴백 모드. 모델은 `[jev].model`로 정합니다. 기본값 `jev-latest`는 공식 SDK의 기본값이자 문서 예시이고, 결과를 고정하고 싶으면 `jev-1.13.0`처럼 버전을 지정하세요.
- 요약·라벨링·기억 정리용으로 설치·로그인된 **`claude` CLI** (`claude -p --model haiku`, 격리 실행 — 보안·문제 해결 참고). jevmem은 사용자 본인의 Claude 로그인(`claude -p`)을 쓰고, `[llm] claude_use_api_key = true`가 아니면 자식 프로세스 환경에서 Anthropic API 키 변수(`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`)를 지웁니다. 이 백그라운드 작업은 30분마다 사람 없이 돌기 때문에, 이런 자동 사용이 본인 Claude 요금제 약관에 맞는지 확인하세요. 요약과 하루 기억 정리(라벨링은 제외)의 폴백으로 [Ollama](https://ollama.com) 로컬 소형 모델(`gemma3`)을 둘 수 있습니다. `[llm] backend`/`fallback`, `[index] labeler = "jev"`로 바꿀 수 있습니다.

## 설치: 처음부터 전체 루프까지

```bash
# 1. 클론해서 pipx 또는 가상환경에 CLI 설치
git clone https://github.com/hojeongna/jev-agent-memory.git
pipx install ./jev-agent-memory
#    또는: python3 -m venv ~/.venvs/jevmem && ~/.venvs/jevmem/bin/pip install ./jev-agent-memory
#    (그냥 `python3 -m pip install .`은 Homebrew·Debian/Ubuntu 파이썬에서
#     "externally-managed-environment"(PEP 668)로 막힘)

# 2. 에이전트 프로젝트 폴더에서 (`claude --channels …`를 실행하는 곳)
cd ~/my-agent
jevmem init --logs memory            # jevmem.toml + .jevmem/ (DB, 예시 분류표)

# 3. 텍스트 LLM: 기본값 backend = "claude"(Haiku), fallback = "ollama", labeler = "haiku"
claude --version                     # claude CLI가 설치·로그인돼 있어야 함
ollama pull gemma3                   # 선택: 요약·기억 정리용 로컬 폴백

# 4. Jev 키는 파일로 (훅과 launchd/cron 모두에서 동작 — 이들은 셸 프로필을 안 읽음)
mkdir -p ~/.config/jevmem && printf '%s' "$YOUR_KEY" > ~/.config/jevmem/key && chmod 600 ~/.config/jevmem/key
#    jevmem.toml → [jev] api_key_file = "~/.config/jevmem/key"

# 5. 분류표를 내 기록에 맞게 (LLM에게 초안을 맡겨도 됨 — docs/taxonomy.md)
$EDITOR .jevmem/taxonomy.json

# 6. 훅: 기록(1단계) + 회상(3단계)
jevmem install-hook                  # .claude/settings.json에 병합 (먼저 백업, 반복 실행 안전)
#    --no-capture: 회상 훅만 (이미 있던 jevmem 기록 훅은 지움)

# 7. 스케줄: 대화 요약(2단계) :04/:34 + 하루 기억 정리 04:00
jevmem schedule install              # launchd plist / crontab 블록 출력만
jevmem schedule install --write      # 바로 등록: 이때부터 claude -p가 30분마다 돔
#    macOS: --no-load는 plist를 쓰기만 하고 적재하지 않음

# 8. 이미 데일리 기록이 있다면 한 번 라벨링 (dry-run이 조각 수와 호출 수를 먼저 보여줌)
jevmem index --dry-run && jevmem index

# 9. 전체 점검
jevmem doctor                        # 키, LLM, 기록 로그, 훅 파이썬, 스케줄, 마지막 실행들
```

**훅과 예약 작업은 설치할 때의 파이썬을 기억합니다.** `install-hook`과 `schedule install`은 자기를 실행한 인터프리터의 절대 경로(`sys.executable`, 예: pipx의 가상환경)를 훅 명령과 launchd/cron 작업에 적어 넣습니다. 그래서 셸 PATH 없이도 돌지만, 그 인터프리터가 사라지면(가상환경 재생성, `pipx reinstall`, 파이썬 업그레이드) `jevmem install-hook`과 `jevmem schedule install --write`를 다시 실행해야 합니다. 훅의 인터프리터가 없으면 `jevmem doctor`가 알려 줍니다. `install-hook --project`로 다른 프로젝트에 설치한 훅도 확인합니다.

이제 평소처럼 채팅하면 됩니다. `jevmem capture tail`로 저장된 메시지를 보고, 다음 :04/:34 실행 뒤 `memory/<오늘>.md`에 `## HH:MM summary` 섹션이 생기고, `.jevmem/inject_log.jsonl`에 메시지마다 무엇을 떠올렸는지 남고, 04:00 이후 `jevmem profile list`에 정리된 판단이 보입니다.

내 데이터 없이 먼저 명령을 써 보고 싶다면, [`examples/`](https://github.com/hojeongna/jev-agent-memory/tree/main/examples)에 지어낸 예시 데이터(데일리 기록, 요약, 기록 로그 `capture.example.jsonl`, 일반 채팅 JSONL, 예시 분류표)가 있습니다. 테스트도 이 데이터를 씁니다. 빈 폴더에서 `jevmem init` 후 `examples/logs/*.md`를 `memory/`에 복사하고 `jevmem index --dry-run`, `jevmem index --no-label`을 실행해 보세요(둘 다 API·`claude`를 호출하지 않음). 그러면 `jevmem search --fts "nightly backup"`으로 백업 기록이 나옵니다. 다만 `jevmem search --fts "backup"`처럼 한 단어로 찾으면 `[fallback] bm25_threshold = 0`으로 두기 전에는 아무것도 나오지 않습니다. 이렇게 작은 기록에서는 단어 하나의 점수가 약해서, 실제 기록 크기에 맞춘 `"auto"` 컷오프를 넘지 못하기 때문입니다. `jevmem raw`와 `jevmem digest --dry-run`도 해 보려면 `examples/summaries/*.md`를 `memory/`에, `examples/capture.example.jsonl`을 `.jevmem/chat.jsonl`(기본 `[capture].path`)로 복사한 뒤 `jevmem raw 2026-09-10 09:34`를 실행하세요.

디버깅 (아래 질문은 예시 데이터 기준):

```bash
jevmem capture tail                                            # 1단계
jevmem digest --dry-run                                        # 2단계: 지금 요약될 메시지 수와 파일 (LLM 호출 없음)
jevmem search "export API는 어떻게 하기로 했지?"                # 3단계 전체 파이프라인 (Jev 호출, 키가 없으면 FTS 폴백)
jevmem search --fts "backup"                                   # 폴백만, API 호출 없음
jevmem raw 2026-09-10 09:34                                    # 요약 뒤의 대화 원문
jevmem profile consolidate --date 2026-09-02 --dry-run         # 보낼 내용만 확인, LLM 호출·쓰기 없음
jevmem index --stats
tail -f .jevmem/inject_log.jsonl .jevmem/logs/*.log            # 훅과 예약 작업이 한 번 돈 뒤에
jevmem install-hook --uninstall ; jevmem schedule uninstall --write
```

모든 설정은 `jevmem.toml`에 있습니다 (임계값, 반감기, 상한, 스케줄 시각, 프롬프트, Jev 지시문, 채팅 소스). 키와 기본값 전체는 **[docs/config-reference.ko.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/config-reference.ko.md)** 에 있습니다. 상대 경로는 설정 파일 위치 기준입니다. 설정 파일은 `--config`, `$JEVMEM_CONFIG`, `./jevmem.toml`, `~/.config/jevmem/jevmem.toml` 순으로 찾고, 뒤의 두 곳처럼 자동으로 찾은 파일은 본인 소유이고 그룹·다른 사용자가 쓸 수 없을 때만 씁니다. `--config`나 `$JEVMEM_CONFIG`로 지정한 파일이 없거나, TOML 문법이 틀리거나, 어느 값이든 틀리면 — 형식이 틀린 값(`max_picks = "four"`, `log_text = "false"`), 범위를 벗어난 숫자, 허용값이 아닌 값(`backend = "claude-sonnet"`), 잘못된 템플릿이나 정규식 — 조용히 기본값으로 돌지 않고 오류로 끝납니다(종료 코드 2). 훅은 실패하는 대신 그 사유를 로그에 남기고(보안 참고), `jevmem doctor`는 FAIL로 보여 줍니다. 설정에 오류가 있는 동안 회상 훅은 아무것도 넣지 않고, 채팅 기록은 `[data]`와 `[capture]`를 읽을 수 있을 때만 계속됩니다(어느 쪽인지는 doctor가 알려 줌). 그러니 **`jevmem.toml`을 고친 뒤와 jevmem을 업그레이드한 뒤에는 `jevmem doctor`를 실행하세요.** 정규식은 작은따옴표(TOML 리터럴 문자열, `'^#{1,4}\s*…'`)로 쓰세요. 큰따옴표 안에서 `\` 하나는 TOML 오류입니다. 모르는 섹션이나 키(`[recal]` 같은 오타)는 오류가 아니지만 `jevmem doctor`가 알려 줍니다. 설정 파일을 아예 찾지 못하면 `init`과 `doctor`를 뺀 모든 명령(`search`, `raw`, `profile`, `capture tail`/`log-out`, `digest`, `index`)이 어디를 찾아봤는지 출력하고 종료 코드 2로 끝나며, 훅은 아무것도 하지 않습니다(실행된 폴더에 데이터를 쓰지 않음). `--config`는 하위 명령 앞뒤 어디에 써도 됩니다: `jevmem --config X profile list`와 `jevmem profile list --config X`는 같습니다.

## 라벨링 상한과 장애

- **실행당:** 최대 `index.cap`(400)개 조각을 라벨링하고, Haiku `claude -p` 호출 1회에 약 20개씩 처리합니다. `jevmem index --dry-run`이 실행 전에 조각 수와 호출 수를 보여 줍니다.
- **재라벨링은 절대 자동으로 일어나지 않습니다.** `--relabel` / `--relabel-topic T`가 조각을 stale로 표시하면, 기존 라벨은 계속 쓰이면서 상한이 걸린 여러 번의 실행에 나눠 처리됩니다.
- **쓸 수 없는 답에는 상한이 있습니다.** 유효한 라벨 없이 돌아온 답이 `index.max_tries`(5)번 쌓인 조각 — Haiku가 분류표 밖의 답을 내거나, Jev가 JSON이 아닌 답·오류 객체·402/429가 아닌 4xx로 답한 경우 — 은 30분마다 다시 시도하지 않고 포기하며, `jevmem index --retry-failed`로 다시 시도합니다. 답 자체를 받지 못한 호출은 세지 않습니다(아래 두 항목).
- **장애 중에는 자동으로 쉬었다 재시도합니다.** 네트워크 끊김, Claude 로그인 만료, 사용량 한도 도달, 타임아웃처럼 `claude -p` 호출 자체가 실패하면 한동안(30분, 1시간, 2시간, 이후 최대 4시간 간격. `[llm] claude_pause_minutes` / `claude_pause_max_minutes`) 호출을 쉬고 스스로 다시 시도합니다. 이런 실패는 `max_tries`에 세지 않고, 한 번 성공하면 쉬는 상태가 풀리며, 그동안 대화 요약은 새 메시지를 모두 원문 줄로 남기고(실행당 `[digest] raw_fallback_lines`까지, 나머지는 다음 실행으로 넘어감), `jevmem doctor`가 쉬는 중임을 보여 줍니다.
- **Jev 장애는 그 실행의 Jev 라벨링만 멈춥니다.** 타임아웃, 연결 실패(연결 거부, DNS), HTTP 5xx·429·402를 받은 Jev 호출은 어느 조각에도 세지 않고, 조각은 대기 상태로 남아 다음 실행에서 다시 시도합니다.
- **Jev 라벨링**(`labeler = "jev"`, 또는 Haiku가 두 번 답했는데도 라벨이 없는 조각의 Jev 폴백)은 호출 예산 상한 `index.max_cost_usd_per_run` / `index.max_cost_usd_per_day`에 맞춰 추가로 잘립니다. `--yes`로 해제합니다.
- **기록 폴더가 없어도 아무것도 지우지 않습니다.** `[logs] dir`이 없거나 읽을 수 없거나(외장 디스크 해제, 잠깐 사라진 동기화 폴더, 오타), `[logs] glob`에 맞는 파일이 하나도 없거나, 색인된 기록 파일 중 2개 이상이면서 절반 넘게 한꺼번에 사라지면(비어서 돌아왔거나 반쯤만 동기화된 폴더), 색인은 이미 있는 조각과 라벨을 모두 그대로 두고 그 사실을 알립니다(`jevmem doctor`에 `logs-missing`). 정말 지우려면 `jevmem index --force-prune`을 쓰세요. 대화 요약은 한 번 있었던 기록 폴더를 다시 만들지 않습니다. 그 실행을 보류하고(`summary: held`) 새 메시지는 기록 로그에서 기다립니다.

## 기본값을 정한 방법

기본값은 원 작성자가 자기 (한국어) 기록과 자기 분류표로 해 본 작고 비공식적이며 블라인드가 아닌 테스트에서 나왔습니다. 여기에 벤치마크 수치는 싣지 않고, jevmem에 동봉한 영어 예시 분류표는 전혀 평가하지 **않았습니다**. jevmem은 내 기록으로 판단해 주세요.

- **회상.** 그 테스트에서 Jev 라우팅·고르기 파이프라인은 그전에 쓰던 키워드 전용 훅보다 쓸모 있는 기억을 더 자주, 노이즈는 더 적게 넣었습니다. Jev 확률은 호출마다 조금씩 달라서, 같은 설정에서도 고르는 조각이 조금 달라질 수 있습니다.
- **백그라운드 모델.** Claude Haiku는 대분류 라벨링에서 Jev와 비슷했고, 계층형 라벨링(주제 먼저, 그다음 그 안의 소분류) 덕분에 소분류가 항상 고른 주제 안에 있습니다. 그래서 기본값은 Haiku가 라벨링과 요약을 맡고, Jev는 속도와 확률이 중요한 메시지별 라우팅·선택에 남겼습니다. 작은 로컬 모델은 더 느리고 약해서(예: 잡담 판별) 요약·기억 정리의 폴백으로만 둡니다.

## 폴백 동작

훅은 항상 exit 0이고 대화에 오류를 출력하지 않습니다. 에피소드 DB 없음, API 키 없음, Jev 오류/타임아웃, 총 시간 `deadline_sec`(4초. 한 바이트씩 느리게 답하는 서버에도 지켜지는 하드 마감) 초과, 402 차단기 열림(Jev가 HTTP 402로 답한 뒤 30분. 그 상태 코드일 때만 열림)일 때 FTS5 폴백을 씁니다(같은 출력 형식, 헤더에 "FTS5 fallback"). 폴백은 3글자 이상 토큰 trigram AND→OR 매칭, 2글자 토큰(한국어에 흔함)은 문서빈도 상한을 둔 `LIKE`, bm25 컷오프(`"auto"`는 기준값 −10.0을 코퍼스 크기에 맞게 조정), 그다음 시간 감쇠로 재정렬합니다. 프로필 블록은 독립적이라, 프로필 DB가 없거나 깨지거나 잠겨 있으면 그 블록만 빠집니다. 설정 파일 자체를 못 읽으면 아무것도 넣지 않고 사유를 훅 오류 로그에 남깁니다(보안 참고). Jev 호출, 잠긴 프로필·에피소드 DB, 폴백 자체의 조회까지 모든 대기는 같은 `deadline_sec` 안에서 쓰이고 마감 0.3초 전에 끝납니다(마감이 재지 않는 파이썬 시작 시간을 위한 여유). 잠긴 에피소드 DB를 기다리는 시간은 최대 3초입니다.

## 분류표 작성

라우팅 품질은 대부분 분류표 품질입니다. **[docs/taxonomy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/taxonomy.md)** 참고. 요약:

- 대분류 6~12개, 소분류 합계 20~60개 정도(최대 255), 기억이 필요 없는 메시지를 위한 **잡담(smalltalk)** 주제 1개.
- 노드마다 설명 두 개: **labeling**(길고 구체적으로, 내 기록에 자주 나오는 명사와 예시) / **routing**(키워드 몇 개).
- Jev는 글을 못 쓰므로, 기록 샘플을 주고 LLM(예: Claude)에게 초안을 쓰게 한 뒤 `jevmem index --stats`로 주제별 개수를 보고 붐비는 칸을 쪼개세요.
- 이름을 바꾸거나 구조를 바꾼 뒤: `jevmem index --relabel --dry-run` → `jevmem index --relabel`.

## 프로필은 어떻게 쌓이나 — 하루 기억 정리(공고화)

하루 기억 정리(`jevmem profile consolidate`, 매일 04:00)는 어제 기록을 읽되 앞 16,000자만 보냅니다(`[consolidate].max_log_chars`로 설정 가능, 그보다 긴 날의 뒷부분은 보지 않음). 텍스트 LLM에게 판단 후보를 최대 3개 엄격한 JSON으로 받은 뒤, 쓰기 전에 **코드로 하나하나 검증**합니다. 종류는 `preference, pattern, constraint, skill, relationship, goal` 중 하나여야 하고, 기분·감정은 거부(근거에 맥락으로만 붙일 수 있음), 인용은 그날 기록에 **글자 그대로** 있어야 하며, merge/supersede 대상은 활성 판단이어야 하고, 기존 판단과 거의 같은 "new"는 merge로 바뀌며, 근거 날짜별 **그리고** 실제 하루 기준 최대 3건입니다. 같은 실행에서 같은 말을 하는 후보 두 개는 판단 하나가 됩니다. 기록이 비어 있으면 LLM을 부르지 않습니다. `--dry-run`은 LLM을 부르지도, 쓰지도 않고 보낼 내용(기록 크기, 기존 판단 수, 남은 예산)만 보여 줍니다. 프롬프트와 규칙: **[docs/profile-consolidation.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/profile-consolidation.md)**.

이 작업이 쓰는 쓰기 명령은 직접(또는 내 에이전트 프롬프트에서) 쓸 수도 있지만, **검사가 더 적습니다.** 허용된 종류인지, merge/supersede 대상이 활성 판단인지, 같은 근거가 이미 있는지, 하루 예산이 남았는지만 봅니다. `--source`가 실제 파일인데 `--quote`가 그 안에 글자 그대로 없으면 경고를 출력하지만 쓰기는 막지 않습니다. 인용이 기록에 그대로 있어야 한다는 규칙, 기분·감정 거부, 거의 같은 "new"를 merge로 바꾸는 것은 하루 기억 정리에만 있습니다.

근거 날짜는 `--source` 파일 **이름**의 `YYYY-MM-DD`입니다(`memory/2026-09-02.md` → 2026-09-02). 그 위 폴더 이름에 든 날짜는 쓰지 않고, 이름에 날짜가 없으면 오늘입니다. 예산 상한은 두 가지이고, 거절할 때 어느 쪽에 걸렸는지 알려 줍니다: 근거 날짜당 3건, 그리고 인용한 날짜와 상관없이 실제 하루에 쓰는 3건. `--backfill`을 붙이면(`new`, `merge`, `supersede`, `consolidate`) 오래된 기록을 처리할 수 있게 하루 총량 상한을 건너뛰고, 근거 날짜당 상한은 그대로 둡니다.

```bash
jevmem profile candidates "짧은 불릿 요약을 선호"     # 비슷한 기존 판단(JSON), 판정은 안 함
jevmem profile merge 12 --source memory/2026-09-02.md --quote "short bullet summaries over long paragraphs"            # dry-run
jevmem profile merge 12 --source memory/2026-09-02.md --quote "short bullet summaries over long paragraphs" --commit
jevmem profile new --kind preference --statement "..." --source ... --quote "..." --commit
jevmem profile supersede 12 --statement "..." --source ... --quote "..." --commit   # 이전 판단은 감사용으로 보존
jevmem profile new --kind goal --statement "..." --source memory/2026-08-15.md --quote "..." --commit --backfill
jevmem profile list
jevmem profile show 12
jevmem profile delete 12 --commit
jevmem profile stats
jevmem profile inject --preview --query "..."
```

## 한계

- **고정된 분류표.** 완전히 새로운 관심사는 가장 비슷한 칸으로 들어가고, 한동안은 희귀어 매칭이 받아줍니다. 위험한 건 낯선 주제를 Jev가 잡담으로 보내 아무것도 안 넣는 경우입니다. 로그에서 잡담으로 분류된 메시지나 확률이 낮은 메시지를 보고 분류표를 늘리세요.
- **라벨 지연.** 마지막 요약 실행 이후의 대화는 아직 회상되지 않습니다 (보통 에이전트가 방금 대화라 이미 알고 있음).
- **지연.** Jev 경로는 메시지당 약 1초 추가. 백그라운드 Haiku 호출은 한 번에 몇 초씩 걸리지만 대화를 막지 않습니다.
- **프로필 매칭은 단어 기반.** 흔한 단어 하나만 겹쳐도 무관한 판단이 붙을 수 있습니다.
- **토크나이저.** 영어와 한국어(조사 제거)용 단순 규칙. 다른 언어는 단어 그대로. FTS trigram은 2글자 토큰을 못 잡아서 `LIKE`로 처리.
- **요약 품질은 텍스트 LLM에 달려 있습니다.** 작은 로컬 모델은 세부를 놓치거나 느슨하게 바꿔 말할 수 있습니다. 프롬프트로 지어내기를 막고 화자·상태·숫자 규칙을 명시하지만 요약 불릿을 자동으로 검증하지는 않습니다 (기억 정리의 인용은 기록과 대조해 검증합니다). 원문 포인터로 에이전트가 요약을 원문과 대조할 수 있습니다. 어떤 오류가 나왔고 무엇을 바꿨는지는 [docs/summary-accuracy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/summary-accuracy.md).
- **원문은 기록을 시작한 뒤부터만 있습니다.** 기록 로그보다 오래된(또는 손으로 쓴) 요약에는 포인터가 붙지 않습니다.
- **기본값은 채팅 앱 대화만 기록합니다.** 터미널에서 에이전트와 한 작업은 `[digest].source`와 `[context].source`를 둘 다 `claude_transcript`로 바꾸지 않는 한 요약되지 않습니다.
- **놓친 하루 기억 정리는 따라잡지 않습니다.** 04:00에 `claude -p`가 장애로 쉬고 있으면 그날 기억 정리는 `llm-unavailable`로 끝납니다. 나중에 `jevmem profile consolidate --date YYYY-MM-DD`로 직접 실행하세요.
- **개인정보:** [컴퓨터 밖으로 나가는 것](#개인정보-컴퓨터-밖으로-나가는-것) 참고.

## 개인정보: 컴퓨터 밖으로 나가는 것

jevmem의 데이터는 내 컴퓨터(`.jevmem/`와 데일리 기록)에 있지만, 아래 네 가지 텍스트는 밖으로 나갑니다.

1. **`claude -p`를 거쳐 Anthropic으로 (대화 요약, 하루 기억 정리, 라벨링).** 대화 요약은 지난 실행 이후의 대화(메시지 거의 전문)를 호출 1번에 최대 24,000자씩 보냅니다. 더 긴 구간은 여러 번에 나눠 전부 보냅니다. 하루 기억 정리는 하루치 기록의 앞 16,000자와 기존 판단 목록을 보냅니다. 라벨링은 기록 조각을 호출당 약 20개씩(조각당 900자까지) 보냅니다.
2. **라벨링 폴백으로 Jev(TypeSafe)에.** Haiku가 두 번 답했는데도 유효한 라벨이 없는 조각(`[index] haiku_max_failures`)은, Jev 키가 있으면 그 조각 내용(최대 1,500자)이 기록 폴더 기준 상대 경로(`[file] 2026-09-10.md` 한 줄)와 함께 Jev로 갑니다. `[index] labeler = "jev"`면 모든 조각이 이렇게 Jev로 갑니다.
3. **회상을 위해 메시지마다 Jev(TypeSafe)에.** 훅은 현재 메시지, 직전 대화 4줄(`[context] lines`), 오늘 날짜(`[recall] include_today`), 날짜를 붙인 200자 기록 발췌 약 100개를 보냅니다.
4. **그룹 채팅의 다른 사람 메시지.** 에이전트가 그룹 채팅에 있으면 다른 사람의 메시지와 표시 이름도 기록(`.jevmem/chat.jsonl`)되고, 요약에 이름(`User (이름)`)과 함께 들어가며, 1~3번으로 함께 나갑니다. 그 사람들에게 알리고 동의를 받는 것은 사용자 책임입니다.

끄는 방법:

- `[hook] log_text = false` — 로컬 회상 로그(`.jevmem/inject_log.jsonl`)에 메시지 본문을 남기지 않습니다.
- `[recall] include_today = false` — Jev로 가는 내용에 날짜 줄을 넣지 않습니다.
- `[context] source = "none"` — 직전 대화 줄을 보내지 않고 현재 메시지만 Jev로 보냅니다.
- `[llm] backend = "ollama"`(또는 `"none"`)와 `fallback = "none"` — 요약과 기억 정리가 Anthropic으로 가지 않습니다(`"none"`이면 원문 줄만, 기억 정리 없음). 라벨링은 `[index] labeler = "jev"`가 아니면 여전히 `claude -p`를 씁니다.
- Jev 키 없음 — Jev로는 아무것도 가지 않습니다. 회상은 로컬 FTS 폴백만 쓰고, 라벨링의 Jev 폴백도 없습니다.
- `[digest] name_speakers = false` — 요약에 보낸 사람 이름을 붙이지 않습니다(채팅 기록에는 여전히 남음).
- jevmem 제거: `jevmem install-hook --uninstall`과 `jevmem schedule uninstall --write`로 기록과 전송이 모두 멈춥니다. `.jevmem/`을 지우면 기록된 대화, 회상 로그, DB가 지워지고, 요약이 써 넣은 `## HH:MM summary` 섹션은 직접 지울 때까지 데일리 기록에 남습니다.

## 보안

- **로컬 데이터는 기본적으로 본인만 읽을 수 있습니다.** jevmem이 새로 만드는 파일(`jevmem init`이 쓰는 `jevmem.toml`과 `.jevmem/taxonomy.json`, `.jevmem/chat.jsonl`, `inject_log.jsonl`, `capture_seen.txt`, SQLite DB, 상태·잠금·장애 대기 파일, 요약이 새로 만드는 데일리 기록)은 권한 0600, 새로 만드는 폴더(`.jevmem/logs/` 포함)는 0700입니다. `.jevmem/hook_settings.json`, `install-hook`이 새로 만드는 `.claude/settings.json`과 그 백업, `schedule install --write`가 쓰는 launchd plist도 마찬가지입니다. 파일을 다시 쓸 때는 비공개 임시 파일(0600, 배타적으로 생성)에 쓰고 디스크에 확실히 기록한 뒤 이름을 바꿉니다. 스케줄러의 `.log` 파일은 launchd/cron이 자기 기본값으로 그 폴더 안에 만듭니다. 이미 있는 파일과 폴더의 권한은 바꾸지 않으니(`.claude/settings.json`을 다시 쓸 때도 원래 권한 유지, 심볼릭 링크인 설정 파일은 링크 그대로), 예전 버전으로 만든 프로젝트는 직접 확인하세요.
- **Jev 키**는 `$TYPESAFE_API_KEY` 또는 키 파일(`chmod 600` 권장, 다른 사용자가 읽을 수 있으면 `jevmem doctor`가 경고)에서 읽고, Jev 클라이언트의 모든 오류 메시지에서 가려집니다. 요청마다 키가 함께 가므로 `[jev] url`은 `https://`만 받고(일반 `http://`는 `127.0.0.1`/`localhost`만), `user@` 부분이나 역슬래시가 든 주소는 거부합니다. 서버가 리다이렉트로 답하면 따라가지 않고(키가 함께 가므로) 오류로 알립니다.
- **설정 파일은 지정했거나 내 것일 때만 믿습니다.** 현재 폴더(또는 `~/.config/jevmem/`)에서 자동으로 찾은 `jevmem.toml`은 본인 소유이고 그룹·다른 사용자가 쓸 수 없을 때만 씁니다(git의 `safe.directory`와 같은 방식). 이 파일이 `claude`로 무엇을 실행할지, Jev 키를 어디로 보낼지 정하기 때문입니다. `[llm] claude_bin`은 명령 이름이나 절대 경로여야 하고, `./claude` 같은 상대 경로는 거부합니다. 훅과 예약 작업은 항상 `--config`로 설정 파일을 지정합니다.
- **백그라운드 `claude -p`** 는 빈 임시 폴더에서, 사용자 본인의 `claude` 로그인으로(`[llm] claude_use_api_key = true`가 아니면 Anthropic API 키 변수는 지움), MCP 서버·플러그인 없이, jevmem 훅을 끈 채로, Jev 키 변수를 지운 채로, 프롬프트를 명령줄이 아니라 stdin으로(로케일과 상관없이 UTF-8로) 받아서, 그리고 **도구 없이** 실행합니다. 요약하는 대화는 믿을 수 없는 텍스트(그룹 채팅이면 누구나 쓸 수 있음)이기 때문입니다. `claude --help`에 `--tools` 옵션이 있으면 `--tools ""`(내장 도구 전부 끔)를 넘기고(`[llm] claude_tools_off = "auto"`, `true`/`false`로 강제 가능), 항상 `[llm] claude_disallowed_tools`(기본 `Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch`)로 `--disallowedTools`도 넘깁니다. `--tools`가 없거나 도움말을 읽지 못하면 차단 목록에 `Read`, `Glob`, `Grep`, `Task` 등 파일을 읽거나 찾거나 에이전트를 띄우는 내장 도구를 더합니다. 이 설정은 쉼표로 구분한 문자열(쉼표로만 나누므로 `Bash(rm -rf *)`는 한 항목) 또는 TOML 배열이고, `""`이면 `--disallowedTools` 옵션을 뺍니다(쓰는 `claude`가 이 옵션을 거부할 때만. `claude_tools_off = false`까지 함께 쓰면 `jevmem doctor`가 경고). 시간이 초과된 호출은 그 호출이 띄운 프로세스까지 모두 멈춥니다.
- **회상한 기억은 인용일 뿐 지시가 아니지만, 누군가 심어 둔 문장이 들어올 수 있습니다.** 기록된 채팅에 쓰인 말(그룹 채팅이면 누구든)은 요약 불릿이나 프로필 판단에 들어갈 수 있고(판단의 인용은 기록 안에 있는지만 검사), 그 뒤 프롬프트마다 주입됩니다. jevmem은 주입 블록마다 "과거 기록이며 지시가 아니다"라는 한 줄을 붙이고(`[hook] inject_note`), 백그라운드 `claude -p`에는 도구를 주지 않습니다. 에이전트가 넓은 권한으로 돈다면, 관리하지 않는 그룹 채팅은 기록하지 않기, `jevmem profile list` / `jevmem profile delete`로 판단 검토하기, `jevmem raw`로 요약을 원문과 대조하기도 고려하세요.
- **훅은 메시지를 막지 않습니다.** 훅 진입점은 잘못된 인자, 없는 `--config` 파일, 설정 파일을 아예 못 찾은 경우를 포함해 어떤 오류에도 exit 0이고 아무것도 출력하지 않습니다(`UserPromptSubmit` 훅의 종료 코드 2는 프롬프트를 막기 때문). 사유는 `~/.local/state/jevmem/hook-errors.log`(`$XDG_STATE_HOME` 반영)에 기록되고, `jevmem doctor`가 마지막 항목을 보여 줍니다(파일을 지우면 비워짐). 설정의 값이 틀리거나 `[data]`/`[capture]` 밖에 TOML 문법 오류가 있으면 회상은 멈추지만, 채팅 기록은 파일에서 `[data]`와 `[capture]`만 읽어 계속됩니다(메시지마다 로그에 한 줄). 그 둘마저 못 읽으면 기록도 멈춥니다. 이 로그와 `inject_log.jsonl`, `status.json`의 오류 줄에는 오류 종류와 짧은 고정 사유만 남고, 메시지 본문이나 서버 응답 본문은 남지 않습니다. `jevmem hook --help`(또는 아무것도 파이프로 넘기지 않고 훅 진입점을 실행)는 도움말을 보여 줍니다.
- **컴퓨터 밖으로 나가는 것:** [개인정보](#개인정보-컴퓨터-밖으로-나가는-것) 참고.

## 문제 해결

**대화 요약이나 기억 정리 직후 채널 플러그인(텔레그램/디스코드) 연결이 끊김.**
사용자 범위 플러그인을 그대로 불러오는 자식 `claude -p`는 채널 플러그인을 하나 더 띄우고, 그게 봇의 메시지 수신(polling)을 가로채서 메인 세션의 플러그인이 끊깁니다. jevmem의 `claude` 백엔드는 항상 빈 임시 폴더에서 `JEVMEM_DISABLE_HOOK=1`로
`claude -p --model haiku --tools "" --disallowedTools … --strict-mcp-config --mcp-config <tmp>/empty.json`
(`{"mcpServers":{}}`, 프롬프트는 stdin)를 실행해서 이를 막습니다. 같은 컴퓨터에서 직접 만든 스크립트로 `claude -p`를 부를 때도 같은 플래그를 쓰세요. `--bare`를 쓰지 않는 이유는 그 모드에서 `claude`가 API 키만 읽고 OAuth 로그인은 읽지 않기 때문입니다. `backend = "ollama"`와 `labeler = "jev"`를 쓰면 `claude -p` 자체를 부르지 않습니다.

**요약이 "(raw)"로만 남음 / 라벨링이 멈춤.** `claude -p` 호출 자체가 실패하면(네트워크, `login`, `usage limit`) `jevmem doctor`에 `claude -p: paused until HH:MM after N failed calls (사유)`가 나옵니다. jevmem이 알아서 기다렸다가(시도 간격 30분~최대 4시간) 다시 시도합니다. 그동안 어느 조각의 라벨링 시도도 세지 않고, 대화 요약은 요약 대신 새 메시지를 모두 원문 줄(`## HH:MM summary (raw)`)로 씁니다. 실행당 최대 `[digest] raw_fallback_lines`(200)줄이고, 넘는 메시지는 기록 로그에 남아 다음 실행이 쓰지 못한 첫 메시지부터 이어서 처리하므로 빠지는 사용자 메시지는 없습니다. 마지막 사용자 메시지 뒤의 어시스턴트·스크립트 메시지 중 상한을 넘는 것만 원문 대신 개수를 적은 한 줄로 남깁니다(기록 로그에는 남아 있어 `jevmem raw`로 볼 수 있음). 원문 섹션을 나중에 다시 요약하지는 않습니다. 사유가 `login`이면 `claude`를 한 번 실행해 다시 로그인하세요.

**"digest_state.json was unreadable" / "continuing from the read position saved in …" / "resuming with chat records from …".** 대화 요약이 어디까지 읽었는지 적어 두는 파일이 비었거나 깨진 경우입니다(예: 정전 직후). 그 파일은 `digest_state.json.bak-…`로 옮겨 두고, 실행마다 함께 쓰는 사본(`digest_state.backup.json`)으로 마지막으로 끝난 실행이 멈춘 곳부터 정확히 이어 갑니다. 사본도 없으면 기록 로그 전체를 다시 요약하는 대신 데일리 기록의 가장 최근 요약 제목 이후 메시지부터 이어서 처리합니다. 시각을 읽을 수 없는 기록은 이 방법으로 자리를 정할 수 없어 건너뛰고, 몇 건인지 경고로 알려 줍니다.

**기록 폴더가 없는데 라벨은 남아 있음("logs-missing", "summary: held").** `[logs] dir`이 없거나 읽을 수 없거나, `[logs] glob`에 맞는 파일이 없거나, 색인된 기록 파일의 절반 넘게 한꺼번에 사라지면 `jevmem doctor`와 대화 요약 출력에 나옵니다. 아무것도 지우지 않고 대화 요약도 쓰지 않으니(새 메시지는 기록 로그에서 기다림) 경로를 고치거나 폴더를 되돌리세요. 사라진 파일의 색인 조각을 정말 지우려면 `jevmem index --force-prune`을 실행하고, 폴더 전체를 일부러 지웠다면 다시 만드세요(`mkdir`).

**`jevmem.toml`을 고치거나 업그레이드한 뒤 `FAIL config`.** 설정에 오류가 있는 동안 회상 훅은 아무것도 넣지 않습니다. `jevmem doctor`는 채팅 기록이 (`[data]`와 `[capture]`만으로) 계속되는지, 멈췄는지도 알려 줍니다. 알려 준 값을 고치고(정규식은 작은따옴표로. 위에 링크한 설정 레퍼런스가 모든 기본값을 그대로 복사할 수 있는 형태로 보여 줌) `jevmem doctor`를 다시 실행하세요.

**"file is not a database" / "database disk image is malformed".** `jevmem doctor`가 손상된 파일을 알려 줍니다. 에피소드 색인(`.jevmem/episodic.sqlite`)은 데일리 기록에서 다시 만들 수 있습니다. 옆으로 옮기고(`mv .jevmem/episodic.sqlite .jevmem/episodic.sqlite.broken`) `jevmem index`를 실행하세요(모든 조각을 다시 라벨링함). 프로필(`.jevmem/profile.sqlite`)은 기록에서 다시 만들 수 없습니다. 백업에서 되살리거나(가끔 복사해 두세요), 옆으로 옮겨 빈 프로필로 새로 시작하세요.

**아무것도 기록되지 않음 / 회상이 멈춤.** `jevmem capture tail`이 비어 있다면 `claude --channels …`를 실행하는 프로젝트에서 `jevmem install-hook`을 했는지, 쓰는 플러그인의 `<channel source="…">`와 reply 도구가 `[capture]`에 있는지 확인하세요. 그다음 `jevmem doctor`를 보면 훅 오류(옮겨진 설정 파일, 잘못된 인자, 설정 오류)와 파이썬이 사라진 훅이 나옵니다. 훅 오류 로그는 모든 프로젝트가 함께 쓰고, 하루가 지난 항목은 info로 보이며, 파일을 지우면 비워집니다.

**가상환경을 다시 만든 뒤 훅이나 예약 작업이 멈춤.** 설치할 때의 파이썬으로 실행되기 때문입니다. `jevmem install-hook`과 `jevmem schedule install --write`를 다시 실행하세요.

**요약이 안 생김.** `jevmem doctor`에 마지막 digest 실행이 나오고, `.jevmem/logs/digest.log`에 스케줄러 출력이 있습니다. launchd/cron은 셸 프로필을 안 읽으니 `[jev].api_key_file`을 쓰고, 스케줄에 적힌 PATH에 `ollama`/`claude`가 있는지 확인하세요. `source = "jsonl"`이면 `[digest] jsonl_path`를 확인하세요. 경로가 비었거나 파일이 없으면 대화 요약이 그렇다고 알려 줍니다.

**조각이 계속 라벨 없이 남음.** `jevmem doctor` / `jevmem index --stats`가 쓸 수 없는 답이 `index.max_tries`번 쌓여 포기한 조각 수를 보여 줍니다(예: 모델이 계속 분류표 밖의 답을 낼 때). 라벨러나 분류표를 고친 뒤 `jevmem index --retry-failed`를 실행하세요. 조각은 있는데 라벨이 하나도 없으면 `doctor`가 경고합니다. Jev 회상에는 라벨이 필요하니 `jevmem index`를 실행하세요.

**회상이 늘 "FTS5 fallback".** `.jevmem/inject_log.jsonl`의 `error` 필드를 보세요 (키 없음, 아직 라벨링된 조각 없음, 타임아웃, HTTP 402 이후 30분 차단기).

## 개발

```bash
python3 -m unittest discover -s tests     # 가짜 Jev 클라이언트·가짜 LLM 백엔드·로컬 테스트 서버만 사용, Jev·Anthropic 호출 없음
```

패키지 이름 `jev-agent-memory`, import/CLI 이름 `jevmem`. 구성(`src/jevmem/`): 1단계 `capture`, 2단계 `digest`·`llm`(텍스트 백엔드)·`chunking`·`labeler`·`indexer`·`store`·`taxonomy`, 3단계 `hook`·`recall`·`raw`·`text`·`jev_client`, 프로필 기억 `profile`·`consolidate`, 설치·운영 `cli`·`config`·`install`·`schedule`·`status`·`fsutil`(비공개 파일, 훅 오류 로그).

## 라이선스

MIT — [LICENSE](https://github.com/hojeongna/jev-agent-memory/blob/main/LICENSE) 참고.
