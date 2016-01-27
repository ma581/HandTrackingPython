"""
FORTH Model based hand tracker.
Single Hand tracking pipeline using FORTH libraries.

"""
#Import numpy %%Manoj
import numpy
import struct
# Core stuff, like containers.
import PyMBVCore as Core
# Image acquisition.
import PyMBVAcquisition as Acquisition
# 3D Multi-hypothesis rendering.
import PyMBVRendering as Rendering
# Conversion of hypotheses to 3D renderables.
import PyMBVDecoding as dec
# A library which puts together the aforementioned
# and some extras to make up 3D hand tracking.
import PyHandTracker as HT

# OpenCV.
import cv2 as cv
# Timing.
from time import clock

import time

if __name__ == '__main__':
    print "Creating Renderer..."
    
    # Opening Pipe ---Manoj
    f = open(r'\\.\pipe\NPtest', 'r+b', 0)
    j = 1
    
    # Turn off logging
    Core.InitLog(['handTracker', 'log.severity', 'error'])
    
    # The 3D renderer is a singleton. The single instance is accessed.
    renderer = Rendering.RendererOGLCudaExposed.get()
    # OpenCV coordinate system is right handed but the renderer's
    # coordinate system is left handed. Conversion is handled, but
    # in the process front facing triangles become back facing triangles.
    # Thus, we inverse the culling order.
    # Try to set it to CullBack or CullNone to see the differences.
    renderer.culling = Rendering.RendererOGLBase.Culling.CullFront
    
    # An exposed renderer is one whose data are exposed through
    # some API. The hand tracker lib requires such a renderer.
    erenderer = Rendering.ExposedRenderer(renderer, renderer)
    
    # Create the hand tracker lib
    # params:
    #   - width (2048): max width preallocated for rendering
    #   - height (2048): max height preallocated for rendering
    #   - tileWidth (64): width of hypothesis rendering tile
    #   - tileHeight (64): height of hypothesis rendering tile
    # With the given parameter the handtracker lib will be able to
    # render at most (2048/64)x(2048x64)=1024 hypotheses in parallel.
    # The greatest this number the more the hypothesis evaluation
    # throughput. Default optimization only requires to render 64
    # hypotheses at a time.
    ht = HT.HandTrackerLib(2048, 2048, 64, 64, erenderer)
    
    # Create a decoder, i.e. an object which can transform
    # 27-D parameter vectors to 3D renderable hands.
    handDec = dec.GenericDecoder()
    # A description for a hand can be found at a file.
    handDec.loadFromFile("media/hand_right_low_RH.xml")
    # Set the decoder to the hand tracker lib.
    ht.decoder = handDec

    # Setup randomization variances to use during heuristic search.
    posvar = [10, 10, 10]               # 3D global translation variance
    rotvar = [0.1, 0.1, 0.1, 0.1]       # Quaternion global rotation variance
    fingervar = [ 0.1, 0.1, 0.1, 0.1]   # Per finger relative angles variance

    # 27-D = 3D position + 4D rotation + 5 x 4D per finger angles.
    ht.variances = Core.DoubleVector( posvar + rotvar + 5 * fingervar)
    
    print "Variances: ",list(ht.variances)
    print "Low Bounds: ",list(ht.lowBounds)
    print "High Bounds: ",list(ht.highBounds)
    print "Randomization Indices: ",list(ht.randomizationIndices)
                 
    # Set the PSO budget, i.e. particles and generations.
    ht.particles = 64
    ht.generations = 25
    
    print "Starting Grabber..."
    
    # Initialize RGBD acquisition. We will be acquiring images
    # from a Kinect2 sensor
    Ch = Acquisition.Kinect2MSGrabber.Channels

    FLAGS = { 'low' : Ch.Depth | Ch.RegisteredColor,
              'high' : Ch.RegisteredDepth | Ch.Color }

    # low or high
    RESOLUTION = 'high'
    acq = Acquisition.Kinect2MSGrabber(FLAGS[RESOLUTION])

    # Initialization for the hand pose of the first frame is specified.
    # If track is lost, resetting will revert track to this pose.
    defaultInitPos = Core.ParamVector([ 0, 80, 900, 0, 0, 1, 0, 1.20946707135219810e-001, 1.57187812868051640e+000, 9.58033504364020840e-003, -1.78593063562731860e-001, 7.89636216585289100e-002, 2.67967456875403400e+000, 1.88385552327860720e-001, 2.20049375319072360e-002, -4.09740579183203310e-002, 1.52145111735213370e+000, 1.48366400350912500e-001, 2.85607073734409630e-002, -4.53781680931323280e-003, 1.52743247624671910e+000, 1.01751907812505270e-001, 1.08706683246161150e-001, 8.10845240231484330e-003, 1.49009228214971090e+000, 4.64716068193632560e-002, -1.44370358851376110e-001])
    
    # The 3D hand pose, as is tracked in the tracking loop.
    currentHandPose = defaultInitPos
    
    # State.
    paused = False
    delay = {True:0,False:1}
    frame = 0
    count=0
    # Tracking is initialized to False. The user should put
    # the right hand so as to match the template and press 'S'
    # to start/reset tracking.
    tracking = False
    actualFPS = 0.0

    print "Entering main Loop."
    while True:
        loopStart = time.time()*1000;
        try:
            # Acquire images and image calibrations and break if unsuccessful.
            imgs, clbs = acq.grab()

            # Take care of the order of images
            if RESOLUTION == 'high':
                [rgb, depth], [clbRgb, clbDepth] = imgs, clbs
            elif RESOLUTION == 'low':
                [depth, rgb], [clbDepth, clbRgb] = imgs, clbs
            else:
                raise RuntimeError('Unsupported acquisition flags')

            # RGB is actually RGBA. Omit alpha channel.
            rgb = rgb[:,:,:3].copy()
        except Exception as e:
            print e
            break

        # Get the depth calibration to extract some basic info.
        width,height = int(clbDepth.width),int(clbDepth.height)
        
        # Step 1: configure 3D rendering to match depth calibration.
        # step1_setupVirtualCamera returns a view matrix and a projection matrix (graphics).
        viewMatrix,projectionMatrix = ht.step1_setupVirtualCamera(clbDepth)
        
        # Step 2: compute the bounding box of the previously tracked hand pose.
        # For the sake of efficiency, search is performed in the vicinity of
        # the previous hand tracking solution. Rendering will be constrained
        # in the bounding box (plus some padding) of the previous tracking solution,
        # in image space.
        # The user might chose to bypass this call and compute a bounding box differently,
        # so as to incorporate other information as well.
        bb = ht.step2_computeBoundingBox(currentHandPose, width, height, 0.1)

        # Step 3: Zoom rendering to given bounding box.
        # The renderer is configures so as to map its projection space
        # to the given bounding box, i.e. zoom in.
        ht.step3_zoomVirtualCamera(projectionMatrix, bb,width,height)
        
        # Step 4: Preprocess input.
        # RGBD frames are processed to as to isolate the hand.
        # This is usually done through skin color detection in the RGB frame.
        # The user might chose to bypass this call and do foreground detection
        # in some other way. What is required is a labels image which is non-zero
        # for foreground and a depth image which contains depth values in mm.
        labels, depths = ht.step4_preprocessInput(rgb, depth, bb)
 
        # Step5: Upload observations for GPU evaluation.
        # Hypothesis testing is performed on the GPU. Therefore, observations
        # are also uploaded to the GPU.
        ht.step5_setObservations(labels, depths)

        fps = 0
        if tracking:
            t = clock()
            # Step 6: Track.
            # Tracking is initialized with the solution for the previous frame
            # and computes the solution for the current frame. The user might
            # chose to initialize tracking from a pose other than the solution
            # from the previous frame. This solution needs to be 27-D for 3D
            # hand tracking with the specified decoder.
            score, currentHandPose = ht.step6_track(currentHandPose)
            t = clock() - t
            fps = 1.0 / t
            

        # Step 7 : Visualize.
        # This call superimposes a hand tracking solution on a RGB image
        viz = ht.step7_visualize(rgb, viewMatrix,projectionMatrix, currentHandPose)
        cv.putText(viz, 'UI FPS = %f, Track FPS = %f' % (actualFPS , fps), (20, 20), 0, 0.5, (0, 0, 255))
        
        cv.imshow("Hand Tracker",viz)

        key = cv.waitKey(delay[paused])
        
        # Press 's' to start/stop tracking.
        if key & 255 == ord('s'):
            tracking = not tracking
            currentHandPose = defaultInitPos
            
        # Press 'q' to quit.
        if key & 255 == ord('q'):
            break
                
        # Press 'p' to pause.
        if key &255 == ord('p'):
            paused = not paused
            


    


        #Getting hand pose data %Manoj ------------------------------
        
        
        #print("currentHandPose =",currentHandPose)
        
        #        # Convert to numpy       
#        currentHandPoseNumpy = numpy.array(currentHandPose)
#        print("currentHandPoseNumpy =", currentHandPoseNumpy)
#        currentHandPose = numpy.array(currentHandPose)


        # get camera calibration
        frustum = clbDepth.camera
        proj = frustum.Graphics_getProjectionTransform()
        view = frustum.Graphics_getViewTransform()
        viewport = frustum.Graphics_getViewportTransform(640, 480)
#        
#        # break down 27-D solution to assembly of 3D transforms
        decoding = ht.decoder.quickDecode(currentHandPose)
#        decoding = numpy.array(decoding)
#        print("decoding=",decoding)
        zero = Core.Vector4(0, 0, 0, 1)
        for d in decoding.values():
            i =0;
            for m in d.matrices:
                # The points computed below have members x, y, z and w
                # Don't neglect w, as some transforms are projective
                # 3D point in homogeneous coordinates
                pt3D = view * m * zero
#                pt3DArray = numpy.array(pt3D)
#                print("pt3DArray=",pt3DArray)
#                print("m =",m)
                i=i+1
#                print((pt3D.x, pt3D.y, pt3D.z, pt3D.w))
                
                
                
#                print("I = ",i)
                
                # 2D projected point in homogeneous coordinates
#                pt2D = viewport * proj * view * m * zero
                
            print("\n")
            
         #Pipework    
        s = 'Message[{0}]'.format(j)
        j += 1
#        f.write(struct.pack('I', len(s)) + s)
#        f.write(struct.pack('f', len(str(pt3D.x))) + str(pt3D.x))
        message = str(pt3D.x) + str('') + str(pt3D.y) + str('') + str(pt3D.z) + str('') + str(pt3D.w)
        
        f.write(struct.pack('I', len(message)) + message)
        f.seek(0)
        print 'Wrote;', pt3D.x
#        print 'Length: ', len(str(pt3D.x)) # len = 13
   #-------------------------------------------------------------           
            
            
            
            
        frame += 1
        loopEnd = time.time()*1000;
        actualFPS = (1000.0/(loopEnd-loopStart))
        
       
