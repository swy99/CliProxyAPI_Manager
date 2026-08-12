# CLIProxyAPI Manager

CLIProxyAPI Manager는 Windows 알림 영역에서
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)를 감시하는 작은
데스크톱 도구입니다. 서버가 종료되면 다시 시작하고, OAuth 인증 만료와 새
CLIProxyAPI 릴리스를 확인합니다.

## 주요 기능

- 15초 간격으로 프로세스와 `/v1/models` 응답 확인
- 서버 종료 시 자동 시작, 3회 연속 응답 실패 시 재시작
- 시작 시 및 6시간 간격으로 공식 CLIProxyAPI 릴리스 확인
- 릴리스 자산의 SHA-256 검증, 업데이트 실패 시 자동 롤백
- OAuth 만료 감지와 공급자별 재로그인 버튼
- Claude Code 전역 서브에이전트 및 Haiku/백그라운드 모델 설정
- CLIProxyAPI 모델 목록 조회와 사용자 정의 모델 ID 직접 입력
- Windows 로그인 시 자동 실행
- 창을 닫아도 계속 동작하는 시스템 트레이 인터페이스

## 원라인 설치

Windows 10/11의 일반 사용자 PowerShell에서 다음 명령을 실행합니다. Node.js가
필요하지 않으므로 새 PC에서는 이 방법을 권장합니다.

```powershell
irm https://raw.githubusercontent.com/swy99/CliProxyAPI_Manager/main/install.ps1 | iex
```

Node.js 18 이상이 이미 설치되어 있으면 npx로 동일한 설치기를 실행할 수 있습니다.

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
- `%LOCALAPPDATA%\CLIProxyAPI`에 backend와 Manager 배치
- 새 설치에 localhost 전용 `config.yaml`과 임의 API 키 생성
- Manager를 Windows 시작프로그램에 등록하고 실행

기존 `config.yaml`은 변경하지 않습니다. 설치가 끝나면 Manager에서 사용할 공급자의
로그인/OAuth를 진행해야 합니다. Claude Code 계정 로그인도 별도 사용자 동작입니다.

### 셸 단축키와 자동 컴팩트 (선택)

설치 마지막에 Claude Code 실행 단축키 추가 여부를 한 번 묻습니다. 기본값은
"아니오"이며, 비대화형(파이프) 실행에서는 묻지 않고 건너뜁니다. 수락하면 다음을
적용합니다.

- Windows PowerShell 프로필과 cmd(doskey)에 단축키 추가
  - `cs` / `csr` / `csw` — `claude --dangerously-skip-permissions` (+ `--resume` / `-w`)
  - `csg` / `csgr` / `csgw` — 위와 같되 `--autocompact 230k`. GPT/Codex 백엔드처럼
    입력 창이 약 258K로 좁은 모델에서 컨텍스트 초과(400)를 피하기 위한 설정입니다.
- `%USERPROFILE%\.claude\settings.json`의 `autoCompactEnabled`를 `true`로 보장

기존 프로필과 설정은 보존하며, 다시 실행해도 중복 없이 갱신합니다(멱등). cmd
단축키는 현재 사용자 범위의 `AutoRun` 레지스트리 값을 통해 로드되며, 기존 값이
있으면 덮어쓰지 않고 이어 붙입니다. 단축키는 새 터미널부터 적용됩니다. 이 단계는
`ANTHROPIC_BASE_URL`이나 모델 배선을 바꾸지 않습니다.

### 설치 옵션

npx에서는 다음 옵션을 사용할 수 있습니다.

```powershell
npx -y cliproxyapi-manager --install-dir "D:\CLIProxyAPI"
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
& "$env:LOCALAPPDATA\CLIProxyAPI\manager\CLIProxyAPI-Manager.exe" --remove-startup
Remove-Item "$env:LOCALAPPDATA\CLIProxyAPI" -Recurse -Force
```

통합 설치기가 설치한 Claude Code는 다른 프로젝트에서도 사용할 수 있으므로 자동으로
제거하지 않습니다.

## Claude Code 전역 모델 설정

관리자 창의 **Claude Code 전역 모델 설정**에서 다음 값을 관리할 수 있습니다.

- **전체 서브에이전트**: `CLAUDE_CODE_SUBAGENT_MODEL`
- **Haiku / 백그라운드**: `ANTHROPIC_DEFAULT_HAIKU_MODEL`

설정은 사용자 전역 파일인 `%USERPROFILE%\.claude\settings.json`에 저장됩니다.
`inherit` 또는 `default`를 선택하면 해당 환경 변수 키를 파일에서 제거해 Claude
Code의 기본 모델 해석을 사용합니다. CLIProxyAPI의 `/v1/models` 응답에 표시되지
않는 모델이나 별칭도 콤보박스에 직접 입력할 수 있습니다.

관리자는 저장 직전에 현재 설정 파일을 다시 읽고 위 두 키만 병합합니다. 저장 중
다른 프로그램의 변경을 감지하면 최신 내용을 다시 읽어 병합을 재시도합니다. 다른
환경 변수와 `permissions`, `hooks` 같은 설정은 보존하며, 기존 파일은
`settings.json.cliproxy-manager.bak`으로 백업한 뒤 원자적으로 교체합니다. JSON이
손상되었거나 `env` 형식이 잘못된 경우에는 원본을 덮어쓰지 않습니다.

프로젝트 `.claude/settings.json`, 로컬 설정, 관리 정책 및 실행 옵션이 사용자 전역
설정보다 우선할 수 있습니다. 저장 후 새 Claude Code 세션을 시작해 적용 상태를
확인하는 것을 권장합니다. 이 기능은 CLIProxyAPI `config.yaml`이나 Claude Code의
Auto mode 분류기 설정을 수정하지 않습니다.

## 인증 만료 처리

관리자는 `config.yaml`의 `auth-dir`에 있는 JSON 파일에서 공급자, 계정 식별자,
만료 시각만 읽습니다. 액세스 토큰과 리프레시 토큰은 로그에 기록하지 않습니다.

CLIProxyAPI가 자체적으로 토큰을 갱신할 시간을 주기 위해 만료 후 5분 동안
기다립니다. 이후에도 만료 상태라면 알림과 재로그인 창을 표시합니다.

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

`v1.1.0`처럼 `v*` 태그를 푸시하면 GitHub Actions가 버전 일치 여부를 확인하고
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
