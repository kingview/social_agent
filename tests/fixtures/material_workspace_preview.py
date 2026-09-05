"""UI snapshots with isolated data; no models, downloads or browser operations."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor
from social_ops_agent.conversation_workspace import ConversationWorkspace
from social_ops_agent.desktop_support import STYLESHEET
from social_ops_agent.settings import LLMSettingsStore
from social_ops_agent.material_desktop import MaterialLibraryDialog,MaterialSettingsDialog
from social_ops_agent.material_ui.strategy_editor import StrategyDialog
from social_ops_agent.material_library import digest_file


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    app=QApplication([]); app.setStyleSheet(STYLESHEET)
    with TemporaryDirectory(prefix='material-ui-preview-') as directory:
        root=Path(directory)
        workspace=ConversationWorkspace(output_root=root,plugin_root=root/'plugins',registry_path=root/'sessions.json',llm_settings_store=LLMSettingsStore(root/'llm.json'))
        job=workspace.material_service.jobs.create('download',['https://example.invalid/1','https://example.invalid/2','https://example.invalid/3'],{},name='下载频道中的图文与视频')
        workspace.material_service.jobs.checkpoint(job,0,{'status':'completed','result':{'output_directory':str(root/'素材下载')}})
        workspace.material_service.jobs.transition(job,'已暂停')
        workspace.resize(1380,900); workspace.show(); app.processEvents(); workspace.task_panel.refresh(); app.processEvents()
        workspace.grab().save(str(args.output/'workbench.png'))
        workspace.pages.setCurrentIndex(1); app.processEvents()
        workspace.grab().save(str(args.output/'toolbox.png'))
        workspace.toolbox.open_tool('import'); app.processEvents()
        workspace.toolbox.dialogs['import'].grab().save(str(args.output/'intake.png'))
        workspace.toolbox.dialogs['import'].close()
        workspace.toolbox.open_tool('discover'); app.processEvents()
        workspace.toolbox.dialogs['discover'].grab().save(str(args.output/'discovery.png'))
        workspace.toolbox.dialogs['discover'].close()
        settings_dialog=MaterialSettingsDialog(workspace.material_service)
        settings_dialog.show(); app.processEvents()
        settings_dialog.grab().save(str(args.output/'material-settings.png')); settings_dialog.close()
        strategy_dialog=StrategyDialog()
        strategy_dialog.show(); app.processEvents()
        strategy_dialog.grab().save(str(args.output/'strategy-editor.png')); strategy_dialog.close()
        # Synthetic data for layout only; this does not exercise real admission quality checks.
        source=root/'layout-sample.png'
        sample=QImage(320,240,QImage.Format.Format_RGB32); sample.fill(QColor('#b8cafa')); sample.save(str(source))
        library=workspace.material_service.library()
        record=library.admit({'source_path':str(source),'candidate_path':str(source),'passed':True,'sha256':digest_file(source),'media_type':'image/png'},metadata={'theme':'科技'})
        library.save_analysis(record['resource_id'],{'confidence':.9,'summary':'仅用于验证素材库布局的测试画面。'},{'quality':80},[{'status':'待配置'}])
        library_dialog=MaterialLibraryDialog(workspace.material_service,workspace.toolbox)
        library_dialog.show(); app.processEvents()
        library_dialog.grab().save(str(args.output/'library.png')); library_dialog.close()
        workspace.pages.setCurrentIndex(0)
        workspace.resize(980,780); app.processEvents()
        workspace.resize(980,780); app.processEvents()
        workspace.grab().save(str(args.output/'workbench-compact.png'))
        workspace.close(); app.processEvents()


if __name__=='__main__':main()
