import unittest
from unittest.mock import MagicMock, patch
from tutti import linearsync, linear

class TestLinearSync(unittest.TestCase):
    @patch('tutti.backlog.locked')
    @patch('tutti.linear.LinearClient')
    def test_sync(self, mock_client_class, mock_locked):
        cfg = {
            'linear': {
                'token': 'test-token',
                'workspace': 'test-workspace',
                'projects': {'tutti': 'P-MIC-1'}
            }
        }
        
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        # Setup mock backlog data
        mock_data = {'tasks': [{'id': 't1', 'title': 'Test task', 'status': 'queued', 'notes': 'some notes'}]}
        mock_locked.return_value.__enter__.return_value = mock_data
        
        # Setup mock Linear response
        mock_client.get_project_issues.return_value = {
            'data': {'project': {'issues': {'nodes': []}}}
        }
        
        linearsync.sync(cfg)
        
        # Assert create_issue was called
        mock_client.create_issue.assert_called()

if __name__ == '__main__':
    unittest.main()
