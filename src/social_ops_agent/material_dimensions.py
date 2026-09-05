"""One registry for scoring configuration, validation and editor labels."""
DIMENSIONS = (('quality','基础质量'),('topic','主题'),('language','语言'),('format','形式'),
              ('audience','受众'),('style','风格'),('timeliness','时效性'),('portrait','人物可见特征'))
SCORE_DIMENSIONS = frozenset(key for key,_ in DIMENSIONS)
FILTER_DIMENSIONS = SCORE_DIMENSIONS - {'quality'}
