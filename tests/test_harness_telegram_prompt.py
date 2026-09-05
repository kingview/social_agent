from social_ops_agent.harness_prompts import execution_persona


def test_latest_telegram_means_bottom_message_not_latest_matching_media():
    prompt = execution_persona()
    assert 'view="latest", max_items=1' in prompt
    assert '不得改选更早的媒体帖' in prompt
