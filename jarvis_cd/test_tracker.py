import itertools
import pickle
import os,sys
from abc import ABC,abstractmethod
import json

class TestTracker(ABC):
    def __init__(self, test_dir, n_steps=0):
        os.makedirs(test_dir, exist_ok=True)
        self.test_dir = test_dir
        self.path = os.path.join(test_dir, 'tracker_log.pkl')
        self.trials = {}
        self.n_steps = n_steps

    def SetVariables(self, *vars):
        self.trials = {tuple(trial):False for trial in itertools.product(*vars)}
        if self.n_steps == 0:
            self.n_steps = len(self.trials)
        self._Restart()

    def _Restart(self):
        if os.path.exists(self.path):
            print(f'Load checkpoint from {self.path}')
            restart_ = pickle.load(open(self.path, 'rb'))
            self.trials.update(restart_)


    def _Checkpoint(self):
        print(f"Checkpoint at {self.path}")
        pickle.dump(self.trials, open(self.path, 'wb'))

    def Run(self):
        """
        PURPOSE:
            Run all experiments, checkpoint after every experiment has completed

        RETURN:
            None
        """

        i = 0
        should_stop=False
        print("\n\n\n\n\n")
        print("-----------------------EXPERIMENT_INIT---------------------------")
        self.ExperimentInit(*self.consts)
        print("--------------------------------------------------")

        for trial,completed in self.trials.items():
            if i % self.n_steps == 0:
                self._Checkpoint()
            if completed is False:
                print("-----------------------TRIAL_INIT---------------------------")
                try:
                    self.TrialInit(*trial)
                    self.trials[trial] = self.Trial(*trial)
                    print("Trial success")
                except Exception as e:
                    self._Checkpoint()
                    should_stop = True
                    print(f'An exception occurred during trial {trial}')
                    print(e)
                print("-----------------------TRIAL_END---------------------------")
                self.TrialEnd(*trial)
                print("--------------------------------------------------")
                if should_stop:
                    break

            i += 1
        print("-----------------------EXPERIMENT_END---------------------------")
        self._Checkpoint()
        self.ExperimentEnd(*self.consts)
        print("-----------------------EXPERIMENT_END---------------------------")

    @abstractmethod
    def ExperimentInit(self):
        """
        PURPOSE:
            Start processes necessary for all trials
            This function is executed once, before any tests have been executed.

        RETURN:
            boolean indicating whether or not initialization was successful
        """
        return

    @abstractmethod
    def ExperimentEnd(self):
        """
        PURPOSE:
            Terminate any processes spawned for this experiment

        RETURN:
            None
        """
        return

    @abstractmethod
    def TrialInit(self):
        """
        PURPOSE:
            Start processes necessary for a particular trial

        RETURN:
            boolean indicating whether or not initialization was successful
        """

        return

    @abstractmethod
    def Trial(self):
        """
        PURPOSE:
            Execute a trial

        RETURN:
            boolean indicating whether or not the trial was successful.
        """
        return

    @abstractmethod
    def TrialEnd(self):
        """
        PURPOSE:
            Terminate any processes spawned for this trial

        RETURN:
            None
        """
        return

    @abstractmethod
    def SaveResults(self):
        return
