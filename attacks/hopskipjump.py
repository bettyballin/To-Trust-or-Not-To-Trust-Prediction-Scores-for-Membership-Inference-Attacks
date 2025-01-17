from tqdm import tqdm
import numpy as np
import torch


class HopSkipJump():
    """
    Implementation of the HopSkipJump attack from Jianbo et al. (2019). This is a powerful black-box attack that
    only requires final class prediction, and is an advanced version of the boundary attack.
    | Paper link: https://arxiv.org/abs/1904.02144
    """

    attack_params = [
        "targeted",
        "norm",
        "max_iter",
        "max_eval",
        "init_eval",
        "init_size",
        "curr_iter",
        "batch_size",
        "verbose",
    ]

    def __init__(
        self,
        classifier,
        apply_softmax,
        input_shape,
        device,
        targeted: bool = False,
        norm=2,
        max_iter: int = 10,
        max_eval: int = 500,
        init_eval: int = 10,
        init_size: int = 100,
        verbose: bool = True,
        rtpt=None
    ):
        self._estimator = classifier
        self.apply_softmax = apply_softmax
        self._input_shape = input_shape
        self.device = device
        self._targeted = targeted
        self.norm = norm
        self.max_iter = max_iter
        self.max_eval = max_eval
        self.init_eval = init_eval
        self.init_size = init_size
        self.curr_iter = 0
        self.batch_size = 1
        self.verbose = verbose
        self._check_params()
        self.curr_iter = 0
        self.rtpt = rtpt

        # Set binary search threshold
        if norm == 2:
            self.theta = 0.01 / np.sqrt(np.prod(self._input_shape))
        else:
            self.theta = 0.01 / np.prod(self._input_shape)

    @property
    def estimator(self):
        return self._estimator

    @property
    def estimator_requirements(self):
        return self._estimator_requirements

    def set_params(self, **kwargs) -> None:
        """
        Take in a dictionary of parameters and apply attack-specific checks before saving them as attributes.
        :param kwargs: A dictionary of attack-specific parameters.
        """
        for key, value in kwargs.items():
            if key in self.attack_params:
                setattr(self, key, value)
        self._check_params()

    @property
    def targeted(self) -> bool:
        """
        Return Boolean if attack is targeted. Return None if not applicable.
        """
        return self._targeted

    @targeted.setter
    def targeted(self, targeted) -> None:
        self._targeted = targeted

    def generate(self, x, y=None, **kwargs):
        """
        Generate adversarial samples and return them in an array.
        :param x: An array with the original inputs to be attacked.
        :param y: Target values (class labels) one-hot-encoded of shape `(nb_samples, nb_classes)` or indices of shape
                (nb_samples,).
        :param mask: An array with a mask broadcastable to input `x` defining where to apply adversarial perturbations.
                    Shape needs to be broadcastable to the shape of x and can also be of the same shape as `x`. Any
                    features for which the mask is zero will not be adversarially perturbed.
        :type mask: `np.ndarray`
        :param x_adv_init: Initial array to act as initial adversarial examples. Same shape as `x`.
        :type x_adv_init: `np.ndarray`
        :param resume: Allow users to continue their previous attack.
        :type resume: `bool`
        :return: An array holding the adversarial examples.
        """
        mask = kwargs.get("mask")

        # Check whether users need a stateful attack
        resume = kwargs.get("resume")

        if resume is not None and resume:
            start = self.curr_iter
        else:
            start = 0

        # Get clip_min and clip_max from the classifier or infer them from data
        clip_min, clip_max = np.min(x), np.max(x)

        # Prediction from the original images
        #preds = self.estimator.predict(x, batch_size=self.batch_size, numpy=True)
        input = torch.from_numpy(x).to(self.device) # [2500, 3, 32, 32]
        output = self.estimator(input)               # [2500, 10]
        if self.apply_softmax:
            output = output.softmax(dim=1)
        preds = torch.argmax(output, dim=1) # [2500]

        # Prediction from the initial adversarial examples if not None
        x_adv_init = kwargs.get("x_adv_init")

        if x_adv_init is not None:
            # Add mask param to the x_adv_init
            for i in range(x.shape[0]):
                if mask[i] is not None:
                    x_adv_init[i] = x_adv_init[i] * mask[i] + x[i] * (1 - mask[i])

            # Do prediction on the init
            init_preds = self.estimator(x_adv_init)

        else:
            init_preds = [None] * len(x)
            x_adv_init = [None] * len(x)

        # Assert that, if attack is targeted, y is provided
        if self.targeted and y is None:
            raise ValueError("Target labels `y` need to be provided for a targeted attack.")

        # Some initial setups
        x_adv = x.astype(float) # [2500, 3, 32, 32]

        # Generate the adversarial samples
        for ind in tqdm(range(len(x_adv)), desc='perturbating samples'):
            val = x_adv[ind]
            self.curr_iter = start

            if self.targeted:
                x_adv[ind] = self._perturb(
                    x=val,
                    y=y[ind],
                    y_p=preds[ind],
                    init_pred=init_preds[ind],
                    adv_init=x_adv_init[ind],
                    clip_min=clip_min,
                    clip_max=clip_max,
                    mask=None
                )

            else:
                x_adv[ind] = self._perturb(
                    x=val,
                    y=-1,
                    y_p=preds[ind],
                    init_pred=init_preds[ind],
                    adv_init=x_adv_init[ind],
                    clip_min=clip_min,
                    clip_max=clip_max,
                    mask=None
                )

            if self.rtpt is not None:
                self.rtpt.step()

        return x_adv

    def _perturb(
        self,
        x: np.ndarray,
        y: int,
        y_p: int,
        init_pred: int,
        adv_init: np.ndarray,
        mask,
        clip_min: float,
        clip_max: float,
    ) -> np.ndarray:
        """
        Internal attack function for one example.
        :param x: An array with one original input to be attacked.
        :param y: If `self.targeted` is true, then `y` represents the target label.
        :param y_p: The predicted label of x.
        :param init_pred: The predicted label of the initial image.
        :param adv_init: Initial array to act as an initial adversarial example.
        :param mask: An array with a mask to be applied to the adversarial perturbations. Shape needs to be
                    broadcastable to the shape of x. Any features for which the mask is zero will not be adversarially
                    perturbed.
        :param clip_min: Minimum value of an example.
        :param clip_max: Maximum value of an example.
        :return: An adversarial example.
        """
        # First, create an initial adversarial sample
        initial_sample = self._init_sample(x, y, y_p, init_pred, adv_init, mask, clip_min, clip_max)

        # If an initial adversarial example is not found, then return the original image
        if initial_sample is None:
            return x

        # If an initial adversarial example found, then go with HopSkipJump attack
        x_adv = self._attack(initial_sample[0], x, initial_sample[1], mask, clip_min, clip_max)

        return x_adv

    def _init_sample(
        self,
        x: np.ndarray,
        y: int,
        y_p: int,
        init_pred: int,
        adv_init: np.ndarray,
        mask,
        clip_min: float,
        clip_max: float
    ):
        """
        Find initial adversarial example for the attack.
        :param x: An array with 1 original input to be attacked.
        :param y: If `self.targeted` is true, then `y` represents the target label.
        :param y_p: The predicted label of x.
        :param init_pred: The predicted label of the initial image.
        :param adv_init: Initial array to act as an initial adversarial example.
        :param mask: An array with a mask to be applied to the adversarial perturbations. Shape needs to be
                    broadcastable to the shape of x. Any features for which the mask is zero will not be adversarially
                    perturbed.
        :param clip_min: Minimum value of an example.
        :param clip_max: Maximum value of an example.
        :return: An adversarial example.
        """
        nprd = np.random.RandomState()
        initial_sample = None

        if self.targeted:
            # Attack satisfied
            if y == y_p:
                return None

            # Attack unsatisfied yet and the initial image satisfied
            if adv_init is not None and init_pred == y:
                return adv_init.astype(float), init_pred

            # Attack unsatisfied yet and the initial image unsatisfied
            for _ in range(self.init_size):
                random_img = nprd.uniform(clip_min, clip_max, size=x.shape).astype(x.dtype)

                if mask is not None:
                    random_img = random_img * mask + x * (1 - mask)

                #random_class = self.estimator(torch.from_numpy(np.array([random_img])))[0]
                input = torch.from_numpy(np.array([random_img])).to(self.device)
                output = self.estimator(input)
                random_class = torch.argmax(output, dim=1)

                if random_class == y:
                    # Binary search to reduce the l2 distance to the original image
                    random_img = self._binary_search(
                        current_sample=random_img,
                        original_sample=x,
                        target=y,
                        norm=2,
                        clip_min=clip_min,
                        clip_max=clip_max,
                        threshold=0.001,
                    )
                    initial_sample = random_img, random_class

        else:
            # The initial image satisfied
            if adv_init is not None and init_pred != y_p:
                return adv_init.astype(float), y_p

            # The initial image unsatisfied
            for _ in range(self.init_size):
                random_img = nprd.uniform(clip_min, clip_max, size=x.shape).astype(x.dtype) # [3, 32, 32]

                if mask is not None:
                    random_img = random_img * mask + x * (1 - mask)

                input = torch.from_numpy(np.array([random_img])).to(self.device, dtype=torch.float) # [3, 32, 32]
                output = self.estimator(input) # [1, 10]
                random_class = torch.argmax(output, dim=1)

                if random_class != y_p:
                    # Binary search to reduce the l2 distance to the original image
                    random_img = self._binary_search(
                        current_sample=random_img,
                        original_sample=x,
                        target=y_p,
                        norm=2,
                        clip_min=clip_min,
                        clip_max=clip_max,
                        threshold=0.001,
                    )
                    initial_sample = random_img, y_p

                    break

        return initial_sample # [  [3, 32, 32],  [1] ]

    def _attack(
        self,
        initial_sample: np.ndarray,
        original_sample: np.ndarray,
        target: int,
        mask,
        clip_min: float,
        clip_max: float,
    ) -> np.ndarray:
        """
        Main function for the boundary attack.
        :param initial_sample: An initial adversarial example.
        :param original_sample: The original input.
        :param target: The target label.
        :param mask: An array with a mask to be applied to the adversarial perturbations. Shape needs to be
                    broadcastable to the shape of x. Any features for which the mask is zero will not be adversarially
                    perturbed.
        :param clip_min: Minimum value of an example.
        :param clip_max: Maximum value of an example.
        :return: an adversarial example.
        """
        # Set current perturbed image to the initial image
        current_sample = initial_sample

        # Main loop to wander around the boundary
        for _ in range(self.max_iter):
            # First compute delta
            delta = self._compute_delta(
                current_sample=current_sample,
                original_sample=original_sample,
                clip_min=clip_min,
                clip_max=clip_max,
            )

            # Then run binary search
            current_sample = self._binary_search(
                current_sample=current_sample,
                original_sample=original_sample,
                norm=self.norm,
                target=target,
                clip_min=clip_min,
                clip_max=clip_max,
            )

            # Next compute the number of evaluations and compute the update
            num_eval = min(int(self.init_eval * np.sqrt(self.curr_iter + 1)), self.max_eval)

            update = self._compute_update(
                current_sample=current_sample,
                num_eval=num_eval,
                delta=delta,
                target=target,
                mask=mask,
                clip_min=clip_min,
                clip_max=clip_max,
            )

            # Finally run step size search by first computing epsilon
            if self.norm == 2:
                dist = np.linalg.norm(original_sample - current_sample)
            else:
                dist = np.max(abs(original_sample - current_sample))

            epsilon = 2.0 * dist / np.sqrt(self.curr_iter + 1)
            success = False

            while not success:
                epsilon /= 2.0
                potential_sample = current_sample + epsilon * update
                success = self._adversarial_satisfactory(
                    samples=potential_sample[None],
                    target=target,
                    clip_min=clip_min,
                    clip_max=clip_max,
                )

            # Update current sample
            current_sample = np.clip(potential_sample, clip_min, clip_max)

            # Update current iteration
            self.curr_iter += 1

        return current_sample

    def _binary_search(
        self,
        current_sample: np.ndarray,
        original_sample: np.ndarray,
        target: int,
        norm,
        clip_min: float,
        clip_max: float,
        threshold=None,
    ) -> np.ndarray:
        """
        Binary search to approach the boundary.
        :param current_sample: Current adversarial example.
        :param original_sample: The original input.
        :param target: The target label.
        :param norm: Order of the norm. Possible values: "inf", np.inf or 2.
        :param clip_min: Minimum value of an example.
        :param clip_max: Maximum value of an example.
        :param threshold: The upper threshold in binary search.
        :return: an adversarial example.
        """
        # First set upper and lower bounds as well as the threshold for the binary search
        if norm == 2:
            (upper_bound, lower_bound) = (1, 0)

            if threshold is None:
                threshold = self.theta

        else:
            (upper_bound, lower_bound) = (
                np.max(abs(original_sample - current_sample)),
                0,
            )

            if threshold is None:
                threshold = np.minimum(upper_bound * self.theta, self.theta)

        # Then start the binary search
        while (upper_bound - lower_bound) > threshold:
            # Interpolation point
            alpha = (upper_bound + lower_bound) / 2.0
            interpolated_sample = self._interpolate(
                current_sample=current_sample,
                original_sample=original_sample,
                alpha=alpha,
                norm=norm,
            )

            # Update upper_bound and lower_bound
            satisfied = self._adversarial_satisfactory(
                samples=interpolated_sample[None],
                target=target,
                clip_min=clip_min,
                clip_max=clip_max,
            )[0]
            lower_bound = np.where(satisfied.cpu() == 0, alpha, lower_bound)
            upper_bound = np.where(satisfied.cpu() == 1, alpha, upper_bound)

        result = self._interpolate(
            current_sample=current_sample,
            original_sample=original_sample,
            alpha=upper_bound,
            norm=norm,
        )

        return result # [3, 32, 32]

    def _compute_delta(
        self,
        current_sample: np.ndarray,
        original_sample: np.ndarray,
        clip_min: float,
        clip_max: float,
    ) -> float:
        """
        Compute the delta parameter.
        :param current_sample: Current adversarial example.
        :param original_sample: The original input.
        :param clip_min: Minimum value of an example.
        :param clip_max: Maximum value of an example.
        :return: Delta value.
        """
        # Note: This is a bit different from the original paper, instead we keep those that are
        # implemented in the original source code of the authors
        if self.curr_iter == 0:
            return 0.1 * (clip_max - clip_min)

        if self.norm == 2:
            dist = np.linalg.norm(original_sample - current_sample)
            delta = np.sqrt(np.prod(self._input_shape)) * self.theta * dist
        else:
            dist = np.max(abs(original_sample - current_sample))
            delta = np.prod(self._input_shape) * self.theta * dist

        return delta

    def _compute_update(
        self,
        current_sample: np.ndarray,
        num_eval: int,
        delta: float,
        target: int,
        mask,
        clip_min: float,
        clip_max: float,
    ) -> np.ndarray:
        """
        Compute the update in Eq.(14).
        :param current_sample: Current adversarial example.
        :param num_eval: The number of evaluations for estimating gradient.
        :param delta: The size of random perturbation.
        :param target: The target label.
        :param mask: An array with a mask to be applied to the adversarial perturbations. Shape needs to be
                    broadcastable to the shape of x. Any features for which the mask is zero will not be adversarially
                    perturbed.
        :param clip_min: Minimum value of an example.
        :param clip_max: Maximum value of an example.
        :return: an updated perturbation.
        """
        # Generate random noise
        rnd_noise_shape = [num_eval] + list(self._input_shape)
        if self.norm == 2:
            rnd_noise = np.random.randn(*rnd_noise_shape).astype(np.float32)
        else:
            rnd_noise = np.random.uniform(low=-1, high=1, size=rnd_noise_shape).astype(np.float32)

        # With mask
        if mask is not None:
            rnd_noise = rnd_noise * mask

        # Normalize random noise to fit into the range of input data
        rnd_noise = rnd_noise / np.sqrt(
            np.sum(rnd_noise**2, axis=tuple(range(len(rnd_noise_shape)))[1:], keepdims=True)
        )
        eval_samples = np.clip(current_sample + delta * rnd_noise, clip_min, clip_max)
        rnd_noise = (eval_samples - current_sample) / delta

        # Compute gradient: This is a bit different from the original paper, instead we keep those that are
        # implemented in the original source code of the authors
        satisfied = self._adversarial_satisfactory(
            samples=eval_samples, target=target, clip_min=clip_min, clip_max=clip_max
        )

        shape = [num_eval] + [1] * len(self._input_shape)

        f_val = 2 * satisfied.reshape([num_eval] + [1] * len(self._input_shape)) - 1.0
        f_val = f_val.cpu().numpy().astype(np.float32)

        if np.mean(f_val) == 1.0:
            grad = np.mean(rnd_noise, axis=0)
        elif np.mean(f_val) == -1.0:
            grad = -np.mean(rnd_noise, axis=0)
        else:
            f_val -= np.mean(f_val)
            grad = np.mean(f_val * rnd_noise, axis=0)

        # Compute update
        if self.norm == 2:
            result = grad / np.linalg.norm(grad)
        else:
            result = np.sign(grad)

        return result

    def _adversarial_satisfactory(
        self, samples: np.ndarray, target: int, clip_min: float, clip_max: float
    ) -> np.ndarray:
        """
        Check whether an image is adversarial.
        :param samples: A batch of examples.
        :param target: The target label.
        :param clip_min: Minimum value of an example.
        :param clip_max: Maximum value of an example.
        :return: An array of 0/1.
        """
        samples = np.clip(samples, clip_min, clip_max)
        input = torch.from_numpy(samples).to(self.device, dtype=torch.float)
        output = self.estimator(input) # [1,10]
        if self.apply_softmax:
            output = output.softmax(dim=1)
        preds = torch.argmax(output, dim=1)

        if self.targeted:
            result = preds == target
        else:
            result = preds != target

        return result

    @staticmethod
    def _interpolate(current_sample, original_sample, alpha, norm):
        """
        Interpolate a new sample based on the original and the current samples.
        :param current_sample: Current adversarial example.
        :param original_sample: The original input.
        :param alpha: The coefficient of interpolation.
        :param norm: Order of the norm. Possible values: "inf", np.inf or 2.
        :return: An adversarial example.
        """
        if norm == 2:
            result = (1 - alpha) * original_sample + alpha * current_sample
        else:
            result = np.clip(current_sample, original_sample - alpha, original_sample + alpha)

        return result

    def _check_params(self) -> None:
        # Check if order of the norm is acceptable given current implementation
        if self.norm not in [2, np.inf, "inf"]:
            raise ValueError('Norm order must be either 2, `np.inf` or "inf".')

        if not isinstance(self.max_iter, (int, np.int)) or self.max_iter < 0:
            raise ValueError("The number of iterations must be a non-negative integer.")

        if not isinstance(self.max_eval, (int, np.int)) or self.max_eval <= 0:
            raise ValueError("The maximum number of evaluations must be a positive integer.")

        if not isinstance(self.init_eval, (int, np.int)) or self.init_eval <= 0:
            raise ValueError("The initial number of evaluations must be a positive integer.")

        if self.init_eval > self.max_eval:
            raise ValueError("The maximum number of evaluations must be larger than the initial number of evaluations.")

        if not isinstance(self.init_size, (int, np.int)) or self.init_size <= 0:
            raise ValueError("The number of initial trials must be a positive integer.")

        if not isinstance(self.verbose, bool):
            raise ValueError("The argument `verbose` has to be of type bool.")
