'''
Kalman Filter Based Tracking
====================
Keys
----
ESC - exit
'''

# System
import numpy as np
import cv2
import sys
import re
from time import clock
from matplotlib import pyplot
from collections import namedtuple
# from pykalman import KalmanFilter

# Project
from beet.track import Track
import beet.tools
# from tools import model_bg2, morph_openclose, cross, handle_keys
import beet.drawing
from beet.drawing import GREEN, RED, BLUE
import beet.keys
from beet.background_subtractor import BackgroundSubtractor

MIN_AREA = 200
MAX_AREA = 1500

FRAME_DELAY = 33

TRANSITION_MATRIX = np.array([[1, 0, 1, 0],
                              [0, 1, 0, 1],
                              [0, 0, 1, 0],
                              [0, 0, 0, 1]], np.float32)

MEASUREMENT_MATRIX = np.array([[1, 0, 0, 0],
                               [0, 1, 0, 0]], np.float32)


class App:
    def __init__(self, video_src="", quiet=False, invisible=False,
                 draw_contours=False, bgsub_thresh=64, draw_tracks=False,
                 draw_frame_num=False, draw_boundary=False, draw_mask=False,
                 set_boundaries=(200, 200, 100, 200)):
        self.roi = (set_boundaries[0], set_boundaries[1])
        self.roi_h = set_boundaries[2]
        self.roi_w = set_boundaries[3]
        self.quiet = quiet
        self.invisible = invisible
        self.draw_contours = draw_contours
        self.threshold = bgsub_thresh
        self.draw_tracks = draw_tracks
        self.draw_frame_num = draw_frame_num
        self.draw_boundary = draw_boundary
        self.draw_mask = draw_mask

        self.areas = []

        # Learn the bg
        self.operator = BackgroundSubtractor(2000, self.threshold, True)
        self.operator.model_bg2(video_src)

        self.cam = cv2.VideoCapture(video_src)

        self.maxTimeInvisible = 0
        self.trackAgeThreshold = 4

        self.tracks = []
        self.lostTracks = []
        self.frame_idx = 0
        self.arrivals = 0
        self.departures = 0

    def run(self, as_script=True):
        if self.invisible:
            cv2.namedWindow("Control")

        self.prev_gray = None
        self.prev_points = []
        self.nextTrackID = 0

        while True:
            frame, fg_mask = self.step()
            if frame is None:
                break
            if not self.invisible:
                cv2.imshow('Tracking', frame)
                if self.draw_mask:
                    cv2.imshow("Mask", fg_mask)
                delay = FRAME_DELAY
                if beet.tools.handle_keys(delay) == 1:
                    break
            # else:
            #     if tools.handle_keys(delay) == 1:
            #         break

            # Should we continue running or yield some information
            # about the current frame
            if as_script:
                continue
            else:
                pass
        # After the video, examine tracks
        # self.checkLostTrackCrosses()
        self.cam.release()

    def step(self):
        # Get frame
        ret, frame = self.cam.read()
        if not ret:
            return None, False
        # Convert frame to grayscale
        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Segment
        fg_mask = self._get_fg_mask(frame)
        # Detect blobs
        contours = self._get_cotours(fg_mask)
        areas, detections = beet.drawing.draw_min_ellipse(
            contours,
            frame, MIN_AREA,
            MAX_AREA, draw=False)
        self.areas += areas

        # Track
        self._track(frame, detections)

        # Store frame and go to next
        self.prev_gray = frame_gray
        self.prev_points = detections
        self.frame_idx += 1
        self.draw_overlays(frame, fg_mask)
        return (frame, fg_mask)

    def _get_fg_mask(self, frame):
        mask = self.operator.apply(frame)
        two_tone = ((mask == 255) * 255).astype(np.uint8)
        morphed = beet.tools.morph_openclose(two_tone)
        return morphed

    def _get_cotours(self, fg_mask):
        version = int(re.findall(r'\d+', cv2.__version__)[0])
        if version == 3:
            _, contours, _ = cv2.findContours((fg_mask.copy()),
                                              cv2.RETR_EXTERNAL,
                                              cv2.CHAIN_APPROX_TC89_L1)
        else:
            # Get contours for detected bees using the foreground mask
            contours, _ = cv2.findContours((fg_mask.copy()),
                                           cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_TC89_L1)
        return contours

    def _track(self, frame, detections):
        self.predictNewLocations(frame)
        assignments, unmatchedTracks, unmatchedDetections = \
            self.assignTracks(detections, frame)
        self.updateMatchedTracks(assignments, detections)
        self.updateUnmatchedTracks(unmatchedTracks)
        self.deleteLostTracks()
        self.createNewTracks(detections, unmatchedDetections)
        self.showTracks(frame)
        # self.showLostTracks(frame)
        self.checkTrackCrosses()

    def deleteLostTracks(self):
        newTracks = []
        tracksLost = 0
        for track in self.tracks:
            # Fraction of tracks age in which is was visible
            visibilty = float(track.totalVisibleCount) / track.age

            # Determine lost tracks
            if not ((track.age < self.trackAgeThreshold and visibilty < .6) or
                    (track.timeInvisible > self.maxTimeInvisible)):  # Valid
                newTracks.append(track)
            else:  # track invalid
                self.lostTracks.append(track)
                tracksLost += 1
        # print("Tracks lost", tracksLost)
        self.tracks = newTracks

    def createNewTracks(self, detections, unmatchedDetections):
        for detectionIndex in unmatchedDetections:
            detection = detections[detectionIndex]
            array_detection = np.array(detection, np.float32)
            # TODO: Create Kalman filter object
            kf = cv2.KalmanFilter(4, 2)
            kf.measurementMatrix = MEASUREMENT_MATRIX
            kf.transitionMatrix = TRANSITION_MATRIX
            # kf.processNoiseCov = PROCESS_NOISE_COV

            # Create the new track
            newTrack = Track(self.nextTrackID, kf)
            newTrack.update(array_detection)
            newTrack.locationHistory.append(detection)
            self.tracks.append(newTrack)
            self.nextTrackID += 1

    def updateMatchedTracks(self, assignments, detections):
        for assignment in assignments:
            trackIndex = assignment.trackIndex
            detectionIndex = assignment.detectionIndex
            detection = detections[detectionIndex]
            array_detection = np.array(detection, np.float32)
            track = self.tracks[trackIndex]

            track.update(array_detection)

            # Update track
            track.age += 1
            track.totalVisibleCount += 1
            track.timeInvisible = 0
            track.locationHistory.append(detection)

    def updateUnmatchedTracks(self, unmatchedTracks):
        for trackIndex in unmatchedTracks:
            track = self.tracks[trackIndex]
            track.age += 1
            track.timeInvisible += 1

    def assignTracks(self, detections, frame):
        """ Returns assignments, unmatchedTracks, unmatchedDetections """
        if len(self.tracks) == 0:
            # There are no tracks, all detections are unmatched
            unmatchedDetections = range(len(detections))
            return [], [], unmatchedDetections
        elif len(detections) == 0:
            # There are no detections, all tracks are unmatched
            unmatchedTracks = range(len(self.tracks))
            return [], unmatchedTracks, []
        else:
            costMatrix = np.zeros((len(self.tracks), len(detections)))
            for i, track in enumerate(self.tracks):
                x1, y1 = track.getPredictedXY()
                for j, (x2, y2) in enumerate(detections):
                    # cv2.line(frame, (x1, y1), (x2, y2), (255, 0, 0))
                    costMatrix[i, j] = np.sqrt((x1 - x2)**2 + (y1 - y2)**2)
            return beet.tools.assignment(costMatrix)

    def predictNewLocations(self, frame):
        for track in self.tracks:
            track.predict(frame)

    def showTracks(self, frame):
        if self.draw_tracks:
            for track in self.tracks:
                track.drawTrack(frame)

    def showLostTracks(self, frame):
        for track in self.lostTracks:
            loc = track.locationHistory[-1]
            cv2.circle(frame, loc, 2, color=(0, 0, 255), thickness=-1)

    def checkTrackCrosses(self):
        for track in self.tracks:
            result = track.checkCrossLastTwo(self.roi, self.roi_w, self.roi_h)
            if result == 1:
                self.arrivals += 1
                # print("Arrival")
            elif result == -1:
                self.departures += 1
                # print("Departure")

    def checkLostTrackCrosses(self):
        self.lostTracks += self.tracks
        for track in self.lostTracks:
            result = track.checkCross()
            if result == 1:
                self.arrivals += 1
                # print("Arrival")
            elif result == -1:
                self.departures += 1
                # print("Departure")

    def draw_overlays(self, frame, fg_mask):
        if self.draw_boundary:
            beet.drawing.draw_rectangle(frame, self.roi,
                                        (self.roi[0]+self.roi_w, self.roi[1]+self.roi_h))
        if self.draw_frame_num:
            beet.drawing.draw_frame_num(frame, self.frame_idx)
        if self.draw_contours:
            pass
            # drawing.draw_contours(frame, fg_mask)

    def openNewVideo(self, video_src):
        self.cam.release()
        self.cam = cv2.VideoCapture(video_src)


def main():
    print(
        "kalman_track.py: This file is not a script.\n" +
        "  Use it via the beet module or use the beet-cli."
    )
    # clock()
    # print("{0} seconds elapsed.".format(timeElapsed))
    # print("FPS: {0}".format(float(app.frame_idx) / timeElapsed))


if __name__ == '__main__':
    main()
