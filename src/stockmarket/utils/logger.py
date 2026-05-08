"""Application logger with dual-channel output (console and UI)."""

import logging
from typing import Optional, List, Dict
from datetime import datetime


class AppLogger:
    """Dual-channel logger for console and Streamlit UI display."""
    
    # Log level emojis
    EMOJI_MAP = {
        "ERROR": "🔴",
        "WARNING": "🟡",
        "INFO": "ℹ️",
        "DEBUG": "🔵",
        "SUCCESS": "✅",
    }
    
    def __init__(self, name: str, level: str = "INFO", max_entries: int = 100):
        """Initialize logger.
        
        Args:
            name: Logger name
            level: Logging level (DEBUG, INFO, WARNING, ERROR)
            max_entries: Maximum number of log entries to keep in memory
        """
        self.name = name
        self.max_entries = max_entries
        self.log_history: List[Dict] = []
        
        # Configure Python logger
        self.logger = logging.getLogger(name)
        self.logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        
        # Add console handler if not already present
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
    
    def log(self, level: str, message: str) -> None:
        """Log message to both console and memory.
        
        Args:
            level: Log level (ERROR, WARNING, INFO, DEBUG, SUCCESS)
            message: Message to log
        """
        level_upper = level.upper()
        
        # Log to Python logger
        log_func = getattr(self.logger, level_upper.lower(), self.logger.info)
        log_func(message)
        
        # Store in memory with emoji prefix
        emoji = self.EMOJI_MAP.get(level_upper, "")
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level_upper,
            "message": message,
            "emoji": emoji,
            "formatted": f"{emoji} **{level_upper}**: {message}"
        }
        self.log_history.append(log_entry)
        
        # Keep history within max_entries limit
        if len(self.log_history) > self.max_entries:
            self.log_history = self.log_history[-self.max_entries:]
    
    def error(self, message: str) -> None:
        """Log error message."""
        self.log("ERROR", message)
    
    def warning(self, message: str) -> None:
        """Log warning message."""
        self.log("WARNING", message)
    
    def info(self, message: str) -> None:
        """Log info message."""
        self.log("INFO", message)
    
    def debug(self, message: str) -> None:
        """Log debug message."""
        self.log("DEBUG", message)
    
    def success(self, message: str) -> None:
        """Log success message."""
        self.log("SUCCESS", message)
    
    def get_ui_logs(self, limit: int = 50) -> List[str]:
        """Get formatted logs for UI display.
        
        Args:
            limit: Number of recent logs to return
            
        Returns:
            List of formatted log strings for Streamlit markdown
        """
        recent_logs = self.log_history[-limit:] if limit else self.log_history
        return [log["formatted"] for log in recent_logs]
    
    def clear_history(self) -> None:
        """Clear log history."""
        self.log_history.clear()
