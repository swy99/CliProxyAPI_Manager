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
- Windows 로그인 시 자동 실행
- 창을 닫아도 계속 동작하는 시스템 트레이 인터페이스

## 설치

1. Releases에서 `CLIProxyAPI-Manager.exe`를 받습니다.
2. 다음 중 한 곳에 파일을 둡니다.
   - `cli-proxy-api.exe`, `config.yaml`과 같은 폴더
   - 위 폴더 바로 아래의 `manager` 폴더
3. `CLIProxyAPI-Manager.exe`를 실행합니다.
4. 창에서 **Windows 로그인 시 자동 실행**을 선택하거나 아래 명령을
   실행합니다.

```powershell
.\CLIProxyAPI-Manager.exe --install-startup
```

등록 해제:

```powershell
.\CLIProxyAPI-Manager.exe --remove-startup
```

관리자 창을 닫으면 트레이로 숨겨집니다. 완전히 끝내려면 트레이 메뉴에서
**관리자 종료 (서버 유지)**를 선택합니다.

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
```

## 로그

- 관리자 로그: 관리자 EXE 옆의 `cliproxy-manager.log`
- 백엔드 표준 출력: CLIProxyAPI 폴더의 `cli-proxy-api.log`
- 백엔드 오류 출력: CLIProxyAPI 폴더의 `cli-proxy-api.error.log`

## 라이선스

MIT
