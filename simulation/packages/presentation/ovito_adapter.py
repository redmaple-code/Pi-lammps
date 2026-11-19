"""Versioned cross-section camera adapter derived from the archived RenderEngine."""
import numpy as np
from ovito.vis import Viewport, TachyonRenderer


def render_section(pipeline, filename, size=(1600,800), frame=0):
    pipeline.add_to_scene()
    try:
        data=pipeline.compute(frame)
        data.cell.vis.enabled=False
        points=np.asarray(data.particles.positions)
        low,high=points.min(axis=0),points.max(axis=0)
        center=(low+high)/2
        extent=high-low
        # OVITO orthographic fov is the half-height in scene units.
        half_height=max(extent[2],extent[0]/(size[0]/size[1]))*.5*1.3
        viewport=Viewport(type=Viewport.Type.Ortho)
        viewport.camera_pos=(center[0],center[1]-max(extent)*2,center[2])
        viewport.camera_dir=(0,1,0)
        viewport.camera_up=(0,0,1)
        viewport.fov=float(half_height)
        viewport.render_image(filename=str(filename),size=size,frame=frame,
                              renderer=TachyonRenderer(ambient_occlusion=True),background=(1,1,1))
        return dict(filename=str(filename),frame=frame,size=size,renderer='tachyon',
                    cameraDirection=[0,1,0],cameraUp=[0,0,1],fovHalfHeight=float(half_height),
                    bboxMin=low.tolist(),bboxMax=high.tolist(),selectedParticles=len(points),
                    adapter='ovito-section/1')
    finally:
        pipeline.remove_from_scene()
