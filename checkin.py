class ClientManager:
    def __init__(self):
        self.clients = {}

    def get_isolated_client(self, account):
        # Create a fresh client for single use
        client = self.create_client()  # Assume create_client is defined
        self.clients[account] = client
        return client

    def __enter__(self):
        # Context manager support for account-specific clients
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # Clean up clients
        for client in self.clients.values():
            client.close()  # Assume each client has a close method

    def check_in_account(self, account):
        # Use isolated client per account
        with self.get_isolated_client(account) as isolated_client:
            isolated_client.cookies = self.get_cookies_for_account(account)  # Assume this method exists
            # Perform check-in operations with isolated_client
        # Cleanup happens automatically when leaving the context manager

    def get_cookies_for_account(self, account):
        # Retrieve and return the cookies for the given account
        pass

    def create_client(self):
        # Logic to create a new client
        pass

    def clean_remaining_clients(self):
        # Cleanup logic for any remaining clients
        pass
