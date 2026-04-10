# Lecture Slide Capture

macOS에서 강의 영상을 재생하는 창을 기준으로 슬라이드가 바뀌는 시점만 자동 저장하는 앱입니다.

저장소 구성:
- `Lecture Slide Capture.app`: 바로 실행 가능한 macOS 앱 번들
- `Lecture Slide Capture.app/Contents/Resources/slide_capture.py`: 핵심 캡처 스크립트
- `Lecture Slide Capture.app/Contents/Resources/requirements.txt`: 자동 설치에 사용하는 Python 의존성 목록

주요 기능:
- Chrome 강의 창 목록을 보고 대상 창을 선택해 캡처
- 슬라이드 전환 시점만 감지해 이미지 저장
- 종료 시 저장된 이미지들을 `slides.pdf`로 묶기
- 기본 저장 경로 기억
- 미리보기 창 제공

실행 방법:
1. `Lecture Slide Capture.app`를 실행합니다.
2. 처음 실행 시 필요한 Python 패키지 설치 여부를 묻는 경우 허용합니다.
3. 메뉴에서 캡처를 시작하거나 창 목록을 먼저 확인합니다.
4. 강의가 끝나면 세션 폴더 안에 캡처 이미지와 PDF가 저장됩니다.

기본 저장 위치:
- 기본값은 `~/Desktop/lecture_captures`
- 실행할 때마다 타임스탬프 하위 폴더가 자동 생성됩니다.

권한 안내:
- macOS의 화면 기록 권한이 필요할 수 있습니다.
- Chrome 창 고정 캡처가 불가능한 환경에서는 내부 로직에 따라 대체 방식으로 동작할 수 있습니다.

확인한 내용:
- `slide_capture.py`는 `python3 -m py_compile`로 문법 검사를 통과했습니다.
- 실행 스크립트 `run_capture_terminal.command`, `run_capture_in_terminal.sh`는 `bash -n` 검사를 통과했습니다.
- `slide_capture.py --help`가 정상 동작하는 것까지 확인했습니다.

주의:
- 실제 캡처 동작은 macOS 권한 상태와 현재 열려 있는 강의 창 상태에 영향을 받습니다.
- 저장소에는 별도 빌드 시스템보다 실행 가능한 앱 번들을 그대로 포함합니다.
