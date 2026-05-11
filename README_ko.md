# Lecture Slide Capture

macOS에서 강의 영상을 재생하는 창을 기준으로 슬라이드가 바뀌는 시점만 자동 저장하는 앱입니다.

저장소 구성:
- `Lecture Slide Capture.app`: 바로 실행 가능한 macOS 앱 번들
- `Lecture Slide Capture.app/Contents/Resources/slide_capture_gui.py`: GUI 프론트엔드
- `Lecture Slide Capture.app/Contents/Resources/slide_capture.py`: 핵심 캡처 스크립트
- `Lecture Slide Capture.app/Contents/Resources/requirements.txt`: 자동 설치에 사용하는 Python 의존성 목록
- `design/lecture-slide-capture-redesign-mockup.png`: GUI 재설계 참고 mockup

주요 기능:
- GUI에서 Chrome 강의 창 또는 화면 영역을 선택해 캡처
- 슬라이드 영역 ROI 선택, 최근 저장 슬라이드, 저장 슬라이드 목록, 세션 로그 확인
- 슬라이드 전환 시점만 감지해 이미지 저장
- 종료 시 저장된 이미지들을 `slides.pdf`로 묶기
- 기본 저장 경로 기억
- 캡처 일시정지/재개와 안전한 종료

실행 방법:
1. `Lecture Slide Capture.app`를 실행합니다.
2. 처음 실행 시 필요한 Python 패키지가 없으면 안내되는 설치 명령을 실행합니다.
3. GUI에서 캡처할 창 또는 화면 영역을 선택하고 슬라이드 영역을 지정합니다.
4. `Start Capture`를 누르면 세션 폴더 안에 캡처 이미지와 PDF가 저장됩니다.

기본 저장 위치:
- 기본값은 `~/Desktop/lecture_captures`
- 실행할 때마다 타임스탬프 하위 폴더가 자동 생성됩니다.

권한 안내:
- macOS의 화면 기록 권한이 필요할 수 있습니다.
- Chrome 창 고정 캡처가 불가능한 환경에서는 내부 로직에 따라 대체 방식으로 동작할 수 있습니다.

확인한 내용:
- `slide_capture.py`와 `slide_capture_gui.py`는 `python3 -m py_compile`로 문법 검사를 통과했습니다.
- 실행 스크립트 `run_capture_terminal.command`, `run_capture_in_terminal.sh`는 `bash -n` 검사를 통과했습니다.
- 앱 런처는 사용 가능한 Python 런타임을 찾아 GUI를 직접 실행합니다.

주의:
- 실제 캡처 동작은 macOS 권한 상태와 현재 열려 있는 강의 창 상태에 영향을 받습니다.
- 저장소에는 별도 빌드 시스템보다 실행 가능한 앱 번들을 그대로 포함합니다.
