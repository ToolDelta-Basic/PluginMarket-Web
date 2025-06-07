class OutPutQueueFullError(Exception):
    def __init__(self, message="The output queue is full and no more content can be added."):
        self.message = message
        super().__init__(self.message)