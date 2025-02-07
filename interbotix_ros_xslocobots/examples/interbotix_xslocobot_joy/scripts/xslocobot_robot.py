#!/usr/bin/env python3

# Copyright 2022 Trossen Robotics
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#
#    * Neither the name of the copyright holder nor the names of its
#      contributors may be used to endorse or promote products derived from
#      this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import argparse
import copy
import sys
from threading import Lock
import time

from interbotix_common_modules.angle_manipulation import angle_manipulation as ang
from interbotix_xs_modules.xs_robot.locobot import InterbotixLocobotXS
from interbotix_xs_msgs.msg import LocobotJoy
import numpy as np
import rclpy
from rclpy.utilities import remove_ros_args


class XSLocobotRobot(InterbotixLocobotXS):
    """
    Processes incoming LocobotJoy messages and outputs robot commands.

    The XSLocobotRobot class is responsible for reading in LocobotJoy messages and sending joint
    and gripper commands to the xs_sdk node, and twist commands to the base; while the arm's
    `waist` joint can be directly controlled via the PS3/PS4 joystick, other buttons allow
    position-ik to be performed using all the arm joints.
    """

    waist_step = 0.06
    current_loop_rate = 25.0
    rotate_step = 0.04
    translate_step = 0.01
    gripper_pressure_step = 0.125
    current_gripper_pressure = 0.5
    loop_rates = {'coarse': 25, 'fine': 25}
    joy_msg = LocobotJoy()
    joy_mutex = Lock()

    def __init__(self, pargs, args=None):
        self.use_base = pargs.use_base
        self.arm_model = 'mobile_' + pargs.robot_model.split('_')[1]
        if self.arm_model == 'mobile_base':
            self.arm_model = None
        InterbotixLocobotXS.__init__(
            self,
            robot_model=pargs.robot_model,
            robot_name=pargs.robot_name,
            arm_model=self.arm_model,
            use_base=self.use_base,
            start_on_init=True,
            args=args
        )
        self.rate = self.core.create_rate(self.current_loop_rate)
        if self.arm_model is not None:
            self.num_joints = self.arm.group_info.num_joints
            self.waist_index = self.arm.group_info.joint_names.index('waist')
            self.waist_ll = self.arm.group_info.joint_lower_limits[self.waist_index]
            self.waist_ul = self.arm.group_info.joint_upper_limits[self.waist_index]
            self.T_sy = np.identity(4)
            self.T_yb = np.identity(4)
            self.update_T_yb()
        self.core.create_subscription(
            msg_type=LocobotJoy,
            topic='commands/joy_processed',
            callback=self.joy_control_cb,
            qos_profile=10,
        )
        time.sleep(0.5)
        self.core.get_logger().info('Ready to receive processed joystick commands.')

    def start_robot(self) -> None:
        try:
            self.start()
            while rclpy.ok():
                self.controller()
                self.rate.sleep()
        except KeyboardInterrupt:
            self.shutdown()

    def update_speed(self, loop_rate: float) -> None:
        """
        Update the frequency at which the main control loop runs.

        :param loop_rate: desired loop frequency [Hz]
        """
        self.current_loop_rate = loop_rate
        self.rate = self.core.create_rate(self.current_loop_rate)
        self.core.get_logger().info(f'Current loop rate is {self.current_loop_rate:0d} Hz.')

    def update_T_yb(self) -> None:
        """Calculate the pose of the end-effector w.r.t. T_y."""
        T_sb = self.arm.get_ee_pose_command()
        rpy = ang.rotation_matrix_to_euler_angles(T_sb[:3, :3])
        self.T_sy[:2, :2] = ang.yaw_to_rotation_matrix(rpy[2])
        self.T_yb = np.dot(ang.trans_inv(self.T_sy), T_sb)

    def update_gripper_pressure(self, gripper_pressure: float) -> None:
        """
        Update gripper pressure.

        :param gripper_pressure: desired gripper pressure from 0 - 1
        """
        self.current_gripper_pressure = gripper_pressure
        self.gripper.set_pressure(self.current_gripper_pressure)
        self.core.get_logger().info(
            f'Gripper pressure is at {(self.current_gripper_pressure * 100.0):2f}%.'
        )

    def joy_control_cb(self, msg: LocobotJoy) -> None:
        """
        Process LocobotJoy messages from ROS Subscription callback.

        :param msg: LocobotJoy ROS message
        """
        with self.joy_mutex:
            self.joy_msg = copy.deepcopy(msg)

        # Check the speed_cmd
        if (msg.speed_cmd == LocobotJoy.SPEED_INC and self.current_loop_rate < 40.0):
            self.update_speed(self.current_loop_rate + 1)
        elif (msg.speed_cmd == LocobotJoy.SPEED_DEC and self.current_loop_rate > 10.0):
            self.update_speed(self.current_loop_rate - 1)

        # Check the speed_toggle_cmd
        if (msg.speed_toggle_cmd == LocobotJoy.SPEED_COARSE):
            self.loop_rates['fine'] = self.current_loop_rate
            self.core.get_logger().info('Switched to Coarse Control')
            self.update_speed(self.loop_rates['coarse'])
        elif (msg.speed_toggle_cmd == LocobotJoy.SPEED_FINE):
            self.loop_rates['coarse'] = self.current_loop_rate
            self.core.get_logger().info('Switched to Fine Control')
            self.update_speed(self.loop_rates['fine'])

        # check base_reset_odom_cmd
        if (msg.base_reset_odom_cmd == LocobotJoy.RESET_ODOM and self.use_base):
            self.base.reset_odom()

        if self.arm_model is None:
            return

        # Check the gripper_cmd
        if (msg.gripper_cmd == LocobotJoy.GRIPPER_RELEASE):
            self.gripper.release(delay=0)
        elif (msg.gripper_cmd == LocobotJoy.GRIPPER_GRASP):
            self.gripper.grasp(delay=0)

        # Check the gripper_pwm_cmd
        if (
            msg.gripper_pwm_cmd == LocobotJoy.GRIPPER_PWM_INC and self.current_gripper_pressure < 1
        ):
            self.update_gripper_pressure(
                self.current_gripper_pressure + self.gripper_pressure_step
            )
        elif (
            msg.gripper_pwm_cmd == LocobotJoy.GRIPPER_PWM_DEC and self.current_gripper_pressure > 0
        ):
            self.update_gripper_pressure(
                self.current_gripper_pressure - self.gripper_pressure_step
            )

    def controller(self) -> None:
        """Run main arm manipulation control loop."""
        with self.joy_mutex:
            msg = copy.deepcopy(self.joy_msg)

        # check if the pan-and-tilt mechanism should be reset
        if (
            msg.pan_cmd == LocobotJoy.PAN_TILT_HOME and msg.tilt_cmd == LocobotJoy.PAN_TILT_HOME
        ):
            self.camera.pan_tilt_go_home(
                pan_profile_velocity=1.0,
                pan_profile_acceleration=0.5,
                tilt_profile_velocity=1.0,
                tilt_profile_acceleration=0.5,
                blocking=False
            )
        # check if the pan/tilt mechanism should be rotated
        elif (msg.pan_cmd != 0 or msg.tilt_cmd != 0):
            cam_positions = self.camera.get_joint_commands()

            if (msg.pan_cmd == LocobotJoy.PAN_CCW):
                cam_positions[0] += self.waist_step
            elif (msg.pan_cmd == LocobotJoy.PAN_CW):
                cam_positions[0] -= self.waist_step

            if (msg.tilt_cmd == LocobotJoy.TILT_UP):
                cam_positions[1] += self.waist_step
            elif (msg.tilt_cmd == LocobotJoy.TILT_DOWN):
                cam_positions[1] -= self.waist_step

            self.camera.pan_tilt_move(
                pan_position=cam_positions[0],
                tilt_position=cam_positions[1],
                pan_profile_velocity=0.2,
                pan_profile_acceleration=0.1,
                tilt_profile_velocity=0.2,
                tilt_profile_acceleration=0.1,
                blocking=False)

        # check mobile base related commands
        if self.use_base:
            self.base.command_velocity_xyaw(
                x=msg.base_x_cmd,
                yaw=msg.base_theta_cmd,
            )

        if self.arm_model is None:
            return

        # Check the pose_cmd
        if (msg.pose_cmd != 0):
            if (msg.pose_cmd == LocobotJoy.HOME_POSE):
                self.arm.go_to_home_pose(moving_time=1.5, accel_time=0.75)
            elif (msg.pose_cmd == LocobotJoy.SLEEP_POSE):
                self.arm.go_to_sleep_pose(moving_time=1.5, accel_time=0.75)
            self.update_T_yb()

        # Check the waist_cmd
        if (msg.waist_cmd != 0):
            waist_position = self.arm.get_single_joint_command(joint_name='waist')
            if (msg.waist_cmd == LocobotJoy.WAIST_CCW):
                success = self.arm.set_single_joint_position(
                    joint_name='waist',
                    position=waist_position + self.waist_step,
                    moving_time=0.2,
                    accel_time=0.1,
                    blocking=False
                )
                if (not success and waist_position != self.waist_ul):
                    self.arm.set_single_joint_position(
                        joint_name='waist',
                        position=self.waist_ul,
                        moving_time=0.2,
                        accel_time=0.1,
                        blocking=False
                    )
            elif (msg.waist_cmd == LocobotJoy.WAIST_CW):
                success = self.arm.set_single_joint_position(
                    joint_name='waist',
                    position=waist_position - self.waist_step,
                    moving_time=0.2,
                    accel_time=0.1,
                    blocking=False
                )
                if (not success and waist_position != self.waist_ll):
                    self.arm.set_single_joint_position(
                        joint_name='waist',
                        position=self.waist_ll,
                        moving_time=0.2,
                        accel_time=0.1,
                        blocking=False
                    )
            self.update_T_yb()

        position_changed = msg.ee_x_cmd + msg.ee_z_cmd
        if (self.num_joints >= 6):
            position_changed += msg.ee_y_cmd
        orientation_changed = msg.ee_roll_cmd + msg.ee_pitch_cmd

        if (position_changed + orientation_changed == 0):
            return

        # Copy the most recent T_yb transform into a temporary variable
        T_yb = np.array(self.T_yb)

        if (position_changed):
            # check ee_x_cmd
            if (msg.ee_x_cmd == LocobotJoy.EE_X_INC):
                T_yb[0, 3] += self.translate_step
            elif (msg.ee_x_cmd == LocobotJoy.EE_X_DEC):
                T_yb[0, 3] -= self.translate_step

            # check ee_y_cmd
            if (
                msg.ee_y_cmd == LocobotJoy.EE_Y_INC and self.num_joints >= 6 and T_yb[0, 3] > 0.3
            ):
                T_yb[1, 3] += self.translate_step
            elif (
                msg.ee_y_cmd == LocobotJoy.EE_Y_DEC and self.num_joints >= 6 and T_yb[0, 3] > 0.3
            ):
                T_yb[1, 3] -= self.translate_step

            # check ee_z_cmd
            if (msg.ee_z_cmd == LocobotJoy.EE_Z_INC):
                T_yb[2, 3] += self.translate_step
            elif (msg.ee_z_cmd == LocobotJoy.EE_Z_DEC):
                T_yb[2, 3] -= self.translate_step

        # check end-effector orientation related commands
        if (orientation_changed != 0):
            rpy = ang.rotation_matrix_to_euler_angles(T_yb[:3, :3])

            # check ee_roll_cmd
            if (msg.ee_roll_cmd == LocobotJoy.EE_ROLL_CCW):
                rpy[0] += self.rotate_step
            elif (msg.ee_roll_cmd == LocobotJoy.EE_ROLL_CW):
                rpy[0] -= self.rotate_step

            # check ee_pitch_cmd
            if (msg.ee_pitch_cmd == LocobotJoy.EE_PITCH_DOWN):
                rpy[1] += self.rotate_step
            elif (msg.ee_pitch_cmd == LocobotJoy.EE_PITCH_UP):
                rpy[1] -= self.rotate_step

            T_yb[:3, :3] = ang.euler_angles_to_rotation_matrix(rpy)

        # Get desired transformation matrix of the end-effector w.r.t. the base frame
        T_sd = np.dot(self.T_sy, T_yb)
        _, success = self.arm.set_ee_pose_matrix(
            T_sd=T_sd,
            custom_guess=self.arm.get_joint_commands(),
            execute=True,
            moving_time=0.2,
            accel_time=0.1,
            blocking=False
        )
        if (success):
            self.T_yb = np.array(T_yb)


def main(args=None):
    p = argparse.ArgumentParser()
    p.add_argument('--robot_model')
    p.add_argument('--robot_name', default='locobot')
    p.add_argument('--use_base', default=False)
    p.add_argument('args', nargs=argparse.REMAINDER)

    command_line_args = remove_ros_args(args=sys.argv)[1:]
    ros_args = p.parse_args(command_line_args)

    bot = XSLocobotRobot(ros_args, args=args)
    bot.start_robot()


if __name__ == '__main__':
    main()
