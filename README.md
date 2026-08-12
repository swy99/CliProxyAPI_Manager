# CLIProxyAPI Manager

CLIProxyAPI Manager는 Windows 알림 영역과 Linux 사용자 서비스에서
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)를 감시하는 작은
데스크톱 도구입니다. 서버가 종료되면 다시 시작하고, OAuth 인증 만료와 새
CLIProxyAPI 릴리스를 확인합니다.

## 주요 기능

- 15초 간격으로 프로세스와 `/v1/models` 응답 확인
- 서버 종료 시 자동 시작, 3회 연속 응답 실패 시 재시작
- 시작 시 및 6시간 간격으로 공식 CLIProxyAPI 릴리스 확인
- 릴리스 자산의 SHA-256 검증, 업데이트 실패 시 자동 롤백
- OAuth 만료 감지와 공급자별 재로그인 버튼
- 선택한 인증 토큰 사용 중지(`auth-disabled` 폴더로 보관 이동)
- Claude Code 전역 서브에이전트 및 Fable/Opus/Sonnet/Haiku 모델 설정
- 현재 CLIProxyAPI 연결을 Claude Code 전역 설정에 안전하게 적용
- CLIProxyAPI 모델 목록 조회와 사용자 정의 모델 ID 직접 입력
- Windows 로그인 시 자동 실행
- 창을 닫아도 계속 동작하는 시스템 트레이 인터페이스
- Linux systemd 사용자 서비스 또는 PID 기반 프로세스 관리
- Linux용 시작·중지·상태·로그·로그인·업데이트 명령

## 원라인 설치

Windows 10/11의 일반 사용자 PowerShell에서 다음 명령을 실행합니다. Node.js가
필요하지 않으므로 새 Windows PC에서는 이 방법을 권장합니다.

```powershell
irm https://raw.githubusercontent.com/swy99/CliProxyAPI_Manager/main/install.ps1 | iex
```

Node.js 18 이상이 이미 설치되어 있으면 npx로 동일한 설치기를 실행할 수 있습니다.
이 명령은 Windows와 Linux를 자동 판별합니다.

```powershell
npx -y cliproxyapi-manager
```

npm에 게시하기 전 저장소 버전을 직접 시험하려면 다음 명령을 사용합니다.

```powershell
npx -y github:swy99/CliProxyAPI_Manager
```

설치기는 다음 작업을 수행합니다.

- 공식 GitHub Release에서 Windows 아키텍처에 맞는 CLIProxyAPI 다운로드
- CLIProxyAPI와 Manager 실행 파일의 SHA-256 검증
- Claude Code가 없을 때 Anthropic 공식 PowerShell 설치기 실행
- `%USERPROFILE%\CLIProxyAPI`에 backend와 Manager 배치
- 새 설치에 localhost 전용 `config.yaml`과 임의 API 키 생성
- Manager를 Windows 시작프로그램에 등록하고 실행

### Linux 설치

Ubuntu/Debian, WSL과 일반적인 Linux 배포판에서 Node.js 18 이상, `curl`, `tar`,
`sha256sum`이 준비되어 있으면 같은 명령을 사용합니다.

```bash
npx -y cliproxyapi-manager
```

Linux에서는 공식 CLIProxyAPI의 `linux_amd64` 또는 `linux_aarch64` 자산을 SHA-256으로
검증한 뒤 `$HOME/CLIProxyAPI`에 설치합니다. GUI EXE 대신 다음 네이티브 관리 명령을
`$HOME/.local/bin`에 연결합니다.

```bash
cliproxyapi-manager start
cliproxyapi-manager stop
cliproxyapi-manager restart
cliproxyapi-manager status
cliproxyapi-manager logs
cliproxyapi-manager login codex
cliproxyapi-manager update
```

사용자 systemd가 가능하면 `cliproxyapi-manager.service`를 등록해 로그인 시 자동
시작하고 장애 시 재시작합니다. WSL·컨테이너처럼 사용자 systemd가 없으면 같은 관리
명령이 PID 기반 백그라운드 실행으로 전환됩니다. `$HOME/.local/bin`이 `PATH`에 없다면
새 셸을 열거나 `export PATH="$HOME/.local/bin:$PATH"`를 셸 설정에 추가하세요.

Linux에서도 기존 설치가 하나면 재사용하고, 서로 다른 설치가 여러 개면 포트 충돌을
막기 위해 중단합니다. `--install-dir`, `--skip-claude-code`, `--skip-startup`,
`--no-launch` 옵션은 Windows와 동일하게 사용할 수 있습니다.

기존 `config.yaml`은 변경하지 않습니다. 설치가 끝나면 Manager에서 사용할 공급자의
로그인/OAuth를 진행해야 합니다. Claude Code 계정 로그인도 별도 사용자 동작입니다.

설치 경로를 생략하면 홈·이전 AppData 기본 경로·시작프로그램 등록·실행 중 프로세스에서
기존 설치를 검색합니다. 하나만 발견되면 그 위치를 그대로 재사용하며, 여러 설치 또는
명시 경로와 다른 기존 설치가 발견되면 포트와 시작프로그램 충돌을 막기 위해 중단합니다.

### 셸 단축키와 자동 컴팩트 (선택)

설치 마지막에 Claude Code 실행 단축키 추가 여부를 한 번 묻습니다. 기본값은
"아니오"이며, 비대화형(파이프) 실행에서는 묻지 않고 건너뜁니다. 수락하면 다음을
적용합니다.

- Windows PowerShell 프로필과 cmd(doskey)에 단축키 추가
  - `cs` / `csr` / `csw` — `claude --dangerously-skip-permissions` (+ `--resume` / `-w`)
  - `csg` / `csgr` / `csgw` — 위와 같되 `--autocompact auto`. Claude Code가 현재
    선택 모델의 컨텍스트 창에 맞춰 자동 압축 임계값을 동적으로 결정합니다.
- `%USERPROFILE%\.claude\settings.json`의 `autoCompactEnabled`를 `true`로 보장

기존 프로필과 설정은 보존하며, 다시 실행해도 중복 없이 갱신합니다(멱등). cmd
단축키는 현재 사용자 범위의 `AutoRun` 레지스트리 값을 통해 로드되며, 기존 값이
있으면 덮어쓰지 않고 이어 붙입니다. 단축키는 새 터미널부터 적용됩니다. 설치기의
이 선택 단계 자체는 `ANTHROPIC_BASE_URL`이나 모델 배선을 바꾸지 않습니다. 연결을
바꾸려면 설치 후 Manager의 **CLIProxyAPI 연결 적용** 버튼을 사용합니다.

### Codex Fast 모델

새 기본 `config.yaml`은 모델 목록에 다음 두 ID를 함께 노출합니다.

- `gpt-5.6-sol` — 일반 서비스 티어
- `gpt-5.6-sol-fast` — 같은 `gpt-5.6-sol`에 `service_tier: priority`를 적용하는 Fast mode alias

CLIProxyAPI의 `oauth-model-alias.codex`에서 `fork: true`로 원본과 alias를 함께 유지하고,
`payload.override`는 fast alias 요청에만 priority 티어를 추가합니다. Codex Spark처럼
별개의 모델로 라우팅하는 기능이 아닙니다. 기존 `config.yaml`은 설치기가 보존하므로,
업그레이드 설치에서는 아래 설정을 기존 항목과 병합해야 합니다.

```yaml
oauth-model-alias:
  codex:
    - name: "gpt-5.6-sol"
      alias: "gpt-5.6-sol-fast"
      display-name: "GPT-5.6 Sol Fast"
      fork: true
      force-mapping: true

payload:
  override:
    - models:
        - name: "gpt-5.6-sol-fast"
          protocol: "codex"
      params:
        service_tier: priority
```

### 설치 옵션

npx에서는 다음 옵션을 사용할 수 있습니다.

```powershell
npx -y cliproxyapi-manager --install-dir "$env:USERPROFILE\CLIProxyAPI"
npx -y cliproxyapi-manager --skip-claude-code
npx -y cliproxyapi-manager --skip-startup --no-launch
```

PowerShell 파일을 직접 실행할 때의 대응 옵션은 `-InstallDir`,
`-SkipClaudeCode`, `-SkipStartupRegistration`, `-NoLaunch`입니다.

설치기를 다시 실행하면 최신 공식 릴리스를 확인하며 기존 설정을 보존합니다. 이전
실행 파일을 교체하는 경우 같은 폴더에 타임스탬프가 포함된 백업을 남깁니다.

### 수동 설치

1. CLIProxyAPI 공식 릴리스에서 `cli-proxy-api.exe`를 준비하고 `config.yaml`을
   같은 폴더에 둡니다.
2. 이 저장소의 Releases에서 `CLIProxyAPI-Manager.exe`를 받습니다.
3. Manager를 다음 중 한 곳에 둡니다.
   - `cli-proxy-api.exe`, `config.yaml`과 같은 폴더
   - 위 폴더 바로 아래의 `manager` 폴더
4. Manager를 실행하고 창에서 **Windows 로그인 시 자동 실행**을 선택합니다.

명령으로 시작프로그램을 관리할 수도 있습니다.

```powershell
.\CLIProxyAPI-Manager.exe --install-startup
.\CLIProxyAPI-Manager.exe --remove-startup
```

관리자 창을 닫으면 트레이로 숨겨집니다. 완전히 끝내려면 트레이 메뉴에서
**관리자 종료 (서버 유지)**를 선택합니다.

### 제거

먼저 시작프로그램 등록을 해제한 뒤 설치 폴더를 삭제합니다. 필요한 경우
`config.yaml`을 먼저 백업하세요. 인증 파일은 별도 기본 경로인
`%USERPROFILE%\.cli-proxy-api`에 있으므로 아래 명령으로 삭제되지 않습니다.

```powershell
& "$env:USERPROFILE\CLIProxyAPI\manager\CLIProxyAPI-Manager.exe" --remove-startup
Remove-Item "$env:USERPROFILE\CLIProxyAPI" -Recurse -Force
```

통합 설치기가 설치한 Claude Code는 다른 프로젝트에서도 사용할 수 있으므로 자동으로
제거하지 않습니다.

## Claude Code 전역 모델 설정

관리자 창의 **Claude Code 전역 모델 설정**에서 다음 값을 관리할 수 있습니다.

- **전체 서브에이전트**: `CLAUDE_CODE_SUBAGENT_MODEL`
- **Fable 기본 모델**: `ANTHROPIC_DEFAULT_FABLE_MODEL`
- **Opus 기본 모델**: `ANTHROPIC_DEFAULT_OPUS_MODEL`
- **Sonnet 기본 모델**: `ANTHROPIC_DEFAULT_SONNET_MODEL`
- **Haiku 기본 모델 / 백그라운드**: `ANTHROPIC_DEFAULT_HAIKU_MODEL`

설정은 사용자 전역 파일인 `%USERPROFILE%\.claude\settings.json`에 저장됩니다.
전체 서브에이전트를 `inherit`로 두면 Explore, Plan 등 각 에이전트가 선택한
`fable`, `opus`, `sonnet`, `haiku` 계층이 네 기본 모델 설정을 통해 실제 모델 ID로
해석됩니다. 전체 서브에이전트에 모델을 지정하면 모든 서브에이전트와 agent team,
workflow agent에 동일한 모델을 강제합니다.

`inherit` 또는 `default`를 선택하면 해당 환경 변수 키를 파일에서 제거해 Claude
Code의 기본 모델 해석을 사용합니다. CLIProxyAPI의 `/v1/models` 응답에 표시되지
않는 모델이나 별칭도 콤보박스에 직접 입력할 수 있습니다.

**CLIProxyAPI 연결 적용**을 누르면 Manager가 최신 `config.yaml`을 다시 읽고 다음
값을 같은 사용자 전역 파일에 저장합니다.

- `ANTHROPIC_BASE_URL` — wildcard 호스트를 접속 가능한 localhost 주소로 정규화한 URL
- `ANTHROPIC_AUTH_TOKEN` — `api-keys`의 첫 번째 유효 키. Claude Code에서
  `Authorization: Bearer` 헤더로 전송됩니다.

CLIProxyAPI가 Bearer 인증을 사용하므로 `ANTHROPIC_API_KEY`를 새로 만들거나 지우지
않습니다. Claude Code의 인증 우선순위에서는 `ANTHROPIC_AUTH_TOKEN`이
`ANTHROPIC_API_KEY`보다 우선합니다. 키 값은 Manager 화면과 로그에 표시하지 않습니다.

관리자는 저장 직전에 현재 설정 파일을 다시 읽고 자신이 관리하는 일곱 키만 병합합니다.
저장 중 다른 프로그램의 변경을 감지하면 최신 내용을 다시 읽어 병합을 재시도합니다.
다른 환경 변수와 `permissions`, `hooks` 같은 설정은 보존하며, 기존 파일은
`settings.json.cliproxy-manager.bak`으로 백업한 뒤 원자적으로 교체합니다. JSON이
손상되었거나 `env` 형식이 잘못된 경우에는 원본을 덮어쓰지 않습니다.

모델 목록 조회, 상태 감시, 재시작, 업데이트 및 로그인 작업도 실행 전에
`config.yaml`을 다시 읽으므로 실행 중 API 키·포트·TLS 설정이 변경되어도 오래된
연결 정보를 계속 사용하지 않습니다. 백엔드 자체가 설정을 다시 읽게 하려면 서버
재시작이 필요할 수 있습니다.

프로젝트 `.claude/settings.json`, 로컬 설정, 관리 정책, 프로세스 환경 변수 및 실행
옵션이 사용자 전역 설정보다 우선할 수 있습니다. 저장 후 새 Claude Code 세션에서
`/status`로 연결과 인증 소스를 확인하는 것을 권장합니다. 이 기능은 CLIProxyAPI
`config.yaml`이나 Claude Code의 Auto mode 분류기 설정을 수정하지 않습니다.

## 인증 만료 처리

관리자는 `config.yaml`의 `auth-dir`에 있는 JSON 파일에서 공급자, 계정 식별자,
만료 시각만 읽습니다. 액세스 토큰과 리프레시 토큰은 로그에 기록하지 않습니다.

CLIProxyAPI가 자체적으로 토큰을 갱신할 시간을 주기 위해 만료 후 5분 동안
기다립니다. 이후에도 만료 상태라면 알림과 재로그인 창을 표시합니다.

인증 토큰 목록에서 항목을 선택하고 **선택 토큰 사용 중지(보관)** 버튼을 누르면
토큰 JSON 파일이 `auth-dir` 옆의 `auth-disabled` 폴더로 이동합니다. 파일 내용은
수정하지 않으므로, 다시 사용하려면 파일을 원래 인증 폴더로 옮기면 됩니다.

## 소스에서 빌드

요구 사항:

- Windows 10 또는 11
- Python 3.11 이상
- PowerShell 5.1 이상

```powershell
.\build.ps1
```

스크립트는 `.manager-venv`를 만들고 단일 파일
`CLIProxyAPI-Manager.exe`를 빌드합니다. 빌드만 하고 시작프로그램에는 등록하지
않으려면 다음과 같이 실행합니다.

```powershell
.\build.ps1 -SkipStartupRegistration
```

테스트:

```powershell
.\.manager-venv\Scripts\python.exe -m unittest discover -s tests -v
npm test
npm pack --dry-run
```

## 릴리스와 npm 게시

`v1.4.0`처럼 `v*` 태그를 푸시하면 GitHub Actions가 버전 일치 여부를 확인하고
다음 자산을 GitHub Release에 게시합니다.

- `CLIProxyAPI-Manager.exe`
- `CLIProxyAPI-Manager.exe.sha256`
- `install.ps1`
- `cliproxyapi-manager-<version>.tgz`

`npx -y cliproxyapi-manager` 명령을 공개하려면 npm 계정으로 최초 한 번 패키지를
게시해야 합니다. 게시 전에 패키지 이름과 포함 파일을 반드시 확인하세요.

```powershell
npm login
npm pack --dry-run
npm publish --access public
```

## 로그

- 관리자 로그: 관리자 EXE 옆의 `cliproxy-manager.log`
- 백엔드 표준 출력: CLIProxyAPI 폴더의 `cli-proxy-api.log`
- 백엔드 오류 출력: CLIProxyAPI 폴더의 `cli-proxy-api.error.log`

## 라이선스

MIT
