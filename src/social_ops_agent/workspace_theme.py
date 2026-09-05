"""Visual tokens aligned with the product document's light workbench/navy rail."""
WORKSPACE_STYLE = '''
QWidget { font-family: "PingFang SC", "Microsoft YaHei", "Arial"; font-size:13px; color:#25334c; }
QMainWindow#conversationWorkspace, QWidget#workspaceShell, QWidget#root,
QWidget#conversationTabs, QWidget#conversationToolbar, QWidget#conversationControls { background:#f6f8fc; }
QWidget#workspaceRail { background:#101c3d; border:none; }
QLabel#workspaceBrand { color:#f5f8ff; font-size:19px; font-weight:750; padding:12px 3px 22px; }
QLabel#railCaption { color:#8e9abc; font-size:11px; padding:18px 8px 4px; }
QLabel#railProfile { color:#b6c3e0; padding:16px 8px; }
QWidget#workspaceRail QPushButton { background:transparent; color:#b5c2df; border:none; text-align:left; padding:0 13px; min-height:42px; border-radius:8px; }
QWidget#workspaceRail QPushButton:checked { background:#2859ed; color:white; }
QWidget#workspaceRail QPushButton:hover { background:#263658; color:white; }
QWidget#workspaceRail QPushButton#conversationAction { border:1px solid #39486a; margin-top:5px; }
QListWidget#railConversations { background:transparent; color:#bbc7e2; border:none; outline:none; }
QListWidget#railConversations::item { border-radius:7px; margin:3px 0; padding:10px 8px; }
QListWidget#railConversations::item:selected { background:#273959; color:white; }
QLabel#workspaceHeading { font-size:20px; font-weight:750; color:#182746; }
QFrame#workspaceHeader { background:white; border-bottom:1px solid #e2e7f0; }
QLabel#title { font-size:28px; font-weight:750; color:#17264a; }
QLabel#subtitle, QLabel#hint { color:#8691a5; font-size:12px; }
QLabel#fieldLabel, QLabel#progressLabel, QLabel#progressValue { color:#53617b; font-weight:600; }
QFrame#card, QFrame#inputFrame, QFrame#progressFrame { background:white; border:1px solid #dfe5f0; border-radius:12px; }
QTextBrowser#chat { background:#f6f8fc; border:none; padding:8px; color:#26334c; font-size:14px; }
QPlainTextEdit#messageInput { background:transparent; border:none; color:#243553; font-size:14px; }
QListWidget#attachmentList { background:#edf3ff; border:1px solid #d6e2ff; border-radius:8px; padding:5px; }
QComboBox#control, QLineEdit#control { min-height:40px; max-height:40px; padding:0 10px; background:white; border:1px solid #d9e0ed; border-radius:8px; color:#314361; font-size:13px; }
QComboBox#control QLineEdit { min-height:36px; max-height:36px; padding:0; background:transparent; border:none; color:#314361; }
QComboBox#control::drop-down { width:28px; border:none; }
QComboBox#control::down-arrow { image:none; }
QListView#comboPopup { background:white; color:#26334c; border:1px solid #d5ddea; border-radius:8px; padding:4px 0; outline:none; }
QListView#comboPopup::item { min-height:42px; padding:0 14px; }
QListView#comboPopup::item:hover { background:#f0f4fd; }
QListView#comboPopup::item:selected { background:#e6eeff; color:#2859ed; }
QPushButton { min-height:38px; padding:0 13px; border-radius:8px; font-weight:600; }
QPushButton#primaryButton { background:#2859ed; color:white; border:1px solid #2859ed; }
QPushButton#primaryButton:hover { background:#1949dc; }
QPushButton#secondaryButton { background:white; color:#5a6982; border:1px solid #dce3ef; }
QPushButton#secondaryButton:hover { background:#edf3ff; color:#2859ed; border-color:#afc3fb; }
QPushButton:disabled { background:#eff2f7; color:#a4adbb; border-color:#e1e6ef; }
QMenu { background:white; color:#34415c; border:1px solid #dce3ef; padding:6px; }
QMenu::item { padding:8px 18px; }
QMenu::item:selected { background:#edf3ff; color:#2859ed; border-radius:5px; }
QProgressBar { min-height:6px; max-height:6px; border:none; border-radius:3px; background:#e4eaf5; }
QProgressBar::chunk { background:#3b6af0; border-radius:3px; }
QFrame#welcomeCard { background:transparent; border:none; }
QLabel#welcomeMark { color:#2859ed; background:#e7eeff; border-radius:23px; font-size:26px; font-weight:800; }
QLabel#welcomeTitle { color:#1e2d50; font-size:24px; font-weight:750; }
QLabel#welcomeSubtitle { color:#8290a8; font-size:13px; }
QPushButton#quickTask { background:white; border:1px solid #e2e7f2; color:#465c83; text-align:left; padding:10px; font-weight:500; }
QPushButton#quickTask:hover { border-color:#aac1fc; background:#f0f5ff; }
QSplitter::handle { background:#e6ebf3; width:1px; }
'''

MATERIAL_STYLE = '''
QWidget { color:#283853; font-size:13px; }
QDialog, QWidget#materialToolbox { background:#f6f8fc; }
QScrollArea, QScrollArea > QWidget > QWidget { background:#f6f8fc; border:none; }
QWidget#materialTasks { background:#fcfdff; border-left:1px solid #e4e9f2; }
QLabel { color:#64738c; border:none; background:transparent; }
QLabel#sectionTitle { font-size:22px; font-weight:750; color:#1d2c4c; }
QLabel#taskHeading { font-size:15px; font-weight:700; color:#243656; }
QLineEdit, QPlainTextEdit, QListWidget, QComboBox, QSpinBox {
 background:white; color:#334662; border:1px solid #dde4ef; border-radius:8px; padding:7px;
}
QListWidget { outline:none; }
QListWidget::item { padding:13px 9px; margin:3px; border:1px solid #e4eaf3; border-radius:8px; }
QListWidget::item:selected { background:#edf3ff; color:#2859ed; border-color:#adc4fe; }
QPushButton { background:white; color:#5e6f89; border:1px solid #d8e1ef; padding:0 10px; }
QPushButton:hover { background:#edf3ff; border-color:#a9bef5; color:#2859ed; }
QPushButton:disabled { background:#f1f4f8; color:#a3adbd; border-color:#e3e8f0; }
QPushButton#primaryButton { background:#2859ed; border-color:#2859ed; color:white; }
QPushButton#taskSegment { font-size:11px; padding:0 5px; min-height:30px; border:none; background:#f0f3f9; }
QPushButton#taskSegment:checked { background:#e4ecff; color:#2859ed; }
QComboBox::drop-down { border:none; width:24px; }
QComboBox::down-arrow { image:none; }
QFrame#toolCard { background:white; border:1px solid #e2e8f2; border-radius:14px; }
QLabel#toolName { font-size:17px; font-weight:700; color:#253655; }
QLabel#toolIcon { font-size:22px; font-weight:700; color:#2859ed; background:#edf2ff; border-radius:10px; }
QLabel#toolState { font-size:11px; color:#8e9bb0; }
'''
