"""The comment wake files (ADR-0023): deploy installs them in every GitHub project repository while the plugin is on."""


def files(config):
    """Hook `deploy.files`: the engine files to install in the project repositories."""
    import deploy
    return deploy.COMMENT_WAKE_FILES
