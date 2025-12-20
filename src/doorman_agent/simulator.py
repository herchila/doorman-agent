class SimulationEnvironment:
    """
    Self-contained simulation environment with Redis and Celery workers.
    Used for demos and testing without external dependencies.
    """
    
    def __init__(self, num_workers: int, logger: StructuredLogger):
        self.num_workers = num_workers
        self.logger = logger
        self.redis_process: Optional[Any] = None
        self.worker_processes: List[Any] = []
        self.redis_port = 6399  # Use non-standard port to avoid conflicts
        self.temp_dir: Optional[str] = None
        
    def _check_redis_server(self) -> bool:
        """Check if redis-server is available"""
        import shutil
        return shutil.which('redis-server') is not None
    
    def _create_celery_app_file(self) -> str:
        """Creates a temporary Celery app file for workers"""
        import tempfile
        
        self.temp_dir = tempfile.mkdtemp(prefix='doorman_sim_')
        app_file = os.path.join(self.temp_dir, 'celery_app.py')
        
        celery_app_code = f'''
import time
from celery import Celery

app = Celery(
    'simulation',
    broker='redis://localhost:{self.redis_port}/0',
    backend='redis://localhost:{self.redis_port}/0'
)

app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    worker_prefetch_multiplier=1,
)

@app.task(name='simulation.demo_task')
def demo_task(duration=5):
    """A simple demo task that sleeps for a given duration"""
    time.sleep(duration)
    return {{"status": "completed", "duration": duration}}

@app.task(name='simulation.quick_task')
def quick_task():
    """A quick task for testing"""
    return {{"status": "done"}}

@app.task(name='simulation.zombie_task')
def zombie_task():
    """A task that runs forever (for zombie simulation)"""
    while True:
        time.sleep(1)
'''
        
        with open(app_file, 'w') as f:
            f.write(celery_app_code)
        
        return app_file
    
    def start_redis(self) -> bool:
        """Starts a local Redis server"""
        import subprocess
        
        if not self._check_redis_server():
            self.logger.error(
                "redis-server not found. Install with: "
                "apt-get install redis-server (Ubuntu) or brew install redis (Mac)"
            )
            return False
        
        try:
            # Start Redis on non-standard port
            self.redis_process = subprocess.Popen(
                ['redis-server', '--port', str(self.redis_port), '--daemonize', 'no', '--loglevel', 'warning'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Wait for Redis to be ready
            time.sleep(1)
            
            # Test connection
            if REDIS_AVAILABLE:
                test_client = redis.from_url(
                    f'redis://localhost:{self.redis_port}/0',
                    socket_timeout=5,
                    socket_connect_timeout=5
                )
                test_client.ping()
                self.logger.info("Simulation Redis started", port=self.redis_port)
                return True
            
        except Exception as e:
            self.logger.error("Failed to start Redis", error=str(e))
            return False
        
        return False
    
    def start_workers(self) -> bool:
        """Starts Celery workers"""
        import subprocess
        
        if self.num_workers <= 0:
            self.logger.info("No workers requested (simulating workers down scenario)")
            return True
        
        app_file = self._create_celery_app_file()
        
        try:
            for i in range(self.num_workers):
                worker_name = f'sim_worker_{i+1}'
                
                # Start Celery worker
                process = subprocess.Popen(
                    [
                        sys.executable, '-m', 'celery',
                        '-A', 'celery_app',
                        'worker',
                        '--loglevel=WARNING',
                        f'--hostname={worker_name}@%h',
                        '--concurrency=1',
                        '-Q', 'celery,default,simulation'
                    ],
                    cwd=self.temp_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, 'PYTHONPATH': self.temp_dir}
                )
                
                self.worker_processes.append(process)
                self.logger.info("Worker started", worker=worker_name, pid=process.pid)
            
            # Wait for workers to register
            time.sleep(3)
            return True
            
        except Exception as e:
            self.logger.error("Failed to start workers", error=str(e))
            return False
    
    def enqueue_tasks(self, count: int = 1, task_type: str = 'quick') -> bool:
        """Enqueues tasks for simulation"""
        if not REDIS_AVAILABLE:
            return False
        
        try:
            # Connect directly to Redis and enqueue Celery-formatted tasks
            client = redis.from_url(
                f'redis://localhost:{self.redis_port}/0',
                decode_responses=True
            )
            
            for i in range(count):
                task_id = f'sim-task-{int(time.time())}-{i}'
                
                # Celery task message format
                task_message = json.dumps({
                    'body': json.dumps([[], {}]),
                    'headers': {
                        'lang': 'py',
                        'task': f'simulation.{task_type}_task',
                        'id': task_id,
                        'timestamp': time.time()
                    },
                    'properties': {
                        'correlation_id': task_id,
                        'delivery_tag': task_id
                    },
                    'content-type': 'application/json',
                    'content-encoding': 'utf-8'
                })
                
                client.lpush('celery', task_message)
            
            self.logger.info("Tasks enqueued", count=count, task_type=task_type)
            return True
            
        except Exception as e:
            self.logger.error("Failed to enqueue tasks", error=str(e))
            return False
    
    def get_config(self) -> Config:
        """Returns a Config object for the simulation environment"""
        config = Config()
        config.api_key = "test"
        config.api_url = "http://localhost:9000"
        config.redis_url = f'redis://localhost:{self.redis_port}/0'
        config.celery_broker_url = f'redis://localhost:{self.redis_port}/0'
        config.check_interval_seconds = 10
        config.alert_cooldown_seconds = 30
        config.monitored_queues = ['celery', 'default', 'simulation']
        config.thresholds.max_queue_size = 10  # Lower thresholds for demo
        config.thresholds.max_wait_time_seconds = 30
        return config
    
    def stop(self):
        """Stops all simulation processes"""
        # Stop workers
        for process in self.worker_processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except:
                process.kill()
        
        # Stop Redis
        if self.redis_process:
            try:
                self.redis_process.terminate()
                self.redis_process.wait(timeout=5)
            except:
                self.redis_process.kill()
        
        # Cleanup temp directory
        if self.temp_dir and os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        
        self.logger.info("Simulation environment stopped")


def run_simulation(num_workers: int, enqueue_tasks: int = 0):
    """
    Runs Doorman in simulation mode with local Redis and configurable workers.
    
    Scenarios:
        --workers 0                    : Workers down scenario (EMERGENCY if tasks pending)
        --workers 0 --enqueue 5        : Workers down + queue filling (EMERGENCY)
        --workers 1                    : Healthy scenario (1 worker)
        --workers 2                    : Healthy scenario (2 workers)
        --workers 1 --enqueue 100      : Backlog scenario (if > threshold)
    """
    logger = StructuredLogger("celery-doorman-sim")
    
    print("\n" + "="*60)
    print("🧪 CELERY DOORMAN - SIMULATION MODE")
    print("="*60)
    print(f"   Workers requested: {num_workers}")
    print(f"   Tasks to enqueue:  {enqueue_tasks}")
    print("="*60 + "\n")
    
    sim = SimulationEnvironment(num_workers, logger)
    
    try:
        # Start Redis
        print("📦 Starting Redis server...")
        if not sim.start_redis():
            print("❌ Failed to start Redis. Is redis-server installed?")
            return
        print("✅ Redis started on port", sim.redis_port)
        
        # Start workers
        if num_workers > 0:
            print(f"\n👷 Starting {num_workers} Celery worker(s)...")
            if not sim.start_workers():
                print("❌ Failed to start workers")
                sim.stop()
                return
            print(f"✅ {num_workers} worker(s) started")
        else:
            print("\n⚠️  No workers started (simulating failure scenario)")
        
        # Enqueue tasks if requested
        if enqueue_tasks > 0:
            print(f"\n📝 Enqueuing {enqueue_tasks} task(s)...")
            if sim.enqueue_tasks(enqueue_tasks):
                print(f"✅ {enqueue_tasks} task(s) enqueued")
            else:
                print("❌ Failed to enqueue tasks")
        
        # Create and run Doorman
        print("\n" + "-"*60)
        print("🚪 Starting Doorman monitoring...")
        print("-"*60 + "\n")
        
        config = sim.get_config()
        # import ipdb; ipdb.set_trace()
        doorman = CeleryDoorman(config)
        
        if not doorman.collector.connect():
            print("❌ Doorman failed to connect")
            sim.stop()
            return
        
        # Run monitoring loop
        doorman.setup_signal_handlers()
        doorman.running = True
        
        check_count = 0
        while doorman.running:
            check_count += 1
            print(f"\n--- Health Check #{check_count} ---")
            
            try:
                metrics = doorman.check_once()
                
                # Print summary
                print(f"   📊 Pending tasks:  {metrics.total_pending_tasks}")
                print(f"   👷 Alive workers:  {metrics.alive_workers}/{metrics.total_workers}")
                print(f"   🔗 Redis:          {'✅' if metrics.redis_connected else '❌'}")
                print(f"   🔗 Celery:         {'✅' if metrics.celery_connected else '❌'}")
                
            except Exception as e:
                logger.error("Check failed", error=str(e))
            
            # Sleep with interrupt check
            for _ in range(config.check_interval_seconds):
                if not doorman.running:
                    break
                time.sleep(1)
        
    except KeyboardInterrupt:
        print("\n\n⏹️  Interrupted by user")
    
    finally:
        print("\n🧹 Cleaning up simulation environment...")
        sim.stop()
        print("✅ Done\n")
