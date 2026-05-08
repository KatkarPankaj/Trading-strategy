"""Storage factory for creating storage backends based on configuration."""

from typing import Literal
from ..utils.config_loader import ConfigLoader
from .file_storage import FileStorageBackend
from .db_storage import DatabaseStorageBackend


class StorageFactory:
    """Factory for creating storage backends based on configuration.
    
    Supports:
    - 'file': JSON file-based storage
    - 'sqlite': SQLite database storage
    - 'postgresql': PostgreSQL database storage (future)
    """
    
    @staticmethod
    def create_storage(storage_type: str = None, config_dir: str = None):
        """Create storage backend based on type.
        
        Args:
            storage_type: Type of storage ('file', 'sqlite', 'postgresql')
                          If None, loads from database_config.json
            config_dir: Path to config directory
            
        Returns:
            StorageBackend instance
            
        Raises:
            ValueError: If storage type is unsupported
        """
        if storage_type is None:
            config_loader = ConfigLoader(config_dir)
            db_config = config_loader.get_database_config()
            storage_type = db_config.get('storage', {}).get('type', 'sqlite')
        
        if storage_type == 'file':
            config_loader = ConfigLoader(config_dir)
            db_config = config_loader.get_database_config()
            storage_path = db_config.get('storage', {}).get('file_storage_path', '.data')
            return FileStorageBackend(storage_path)
        
        elif storage_type == 'sqlite':
            config_loader = ConfigLoader(config_dir)
            db_config = config_loader.get_database_config()
            db_path = db_config.get('storage', {}).get('database_path', '.database/trading.db')
            return DatabaseStorageBackend(db_path)
        
        elif storage_type == 'postgresql':
            # Future implementation
            raise NotImplementedError("PostgreSQL backend not yet implemented")
        
        else:
            raise ValueError(f"Unsupported storage type: {storage_type}")
