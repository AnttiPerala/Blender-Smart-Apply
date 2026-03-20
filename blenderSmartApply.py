bl_info = {
    "name": "Smart Apply Scene",
    "author": "Assistant",
    "version": (8, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > N-Panel > Clean Scene",
    "description": "Applies transforms with hierarchy safety and animation-aware options.",
    "category": "Object",
}

import bpy
import traceback

class OBJECT_OT_smart_apply_instant(bpy.types.Operator):
    """Safely apply transforms with animation-aware handling"""
    bl_idname = "object.smart_apply_instant"
    bl_label = "Smart Apply (Instant)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        props = scene.smart_apply_props
        wm = context.window_manager
        window = context.window
        targets = []
        skipped_animated = 0
        animation_warnings = 0

        def ensure_object_mode():
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')

        def select_and_activate(objects):
            bpy.ops.object.select_all(action='DESELECT')
            for obj in objects:
                obj.select_set(True)
            if objects:
                context.view_layer.objects.active = objects[0]

        # Start Progress
        wm.progress_begin(0, 100)
        if window:
            window.cursor_modal_set('WAIT')

        try:
            ensure_object_mode()

            # --- 1. PREPARATION ---
            print("Step 1: Analyzing Scene...")
            valid_types = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META', 'EMPTY', 'ARMATURE', 'LIGHT', 'CAMERA', 'SPEAKER'}
            visibility_state = {}
            
            view_layer_objects = context.view_layer.objects
            
            for obj in view_layer_objects:
                if obj.type in valid_types:
                    # Handle Visibility
                    is_global_hidden = obj.hide_viewport
                    is_local_hidden = obj.hide_get()
                    
                    if is_global_hidden or is_local_hidden:
                        visibility_state[obj] = {'global': is_global_hidden, 'local': is_local_hidden}
                        obj.hide_viewport = False
                        obj.hide_set(False)
                    
                    targets.append(obj)
                    
            if not targets:
                self.report({'WARNING'}, "No objects found.")
                return {'CANCELLED'}

            # --- 2. RECORD HIERARCHY ---
            wm.progress_update(10)
            
            relationships = {}
            for obj in targets:
                has_anim = bool(
                    obj.animation_data and
                    obj.animation_data.action and
                    obj.animation_data.action.fcurves
                )
                relationships[obj] = {
                    "parent": obj.parent,
                    "parent_type": obj.parent_type,
                    "parent_bone": obj.parent_bone,
                    "has_animation": has_anim,
                    # Store ORIGINAL World Matrix. This is the absolute truth of where the object is.
                    "stored_matrix_world": obj.matrix_world.copy(),
                    # Store pre-apply local transforms for animation correction
                    "stored_loc": obj.location.copy(),
                    "stored_rot_euler": obj.rotation_euler.copy() if obj.rotation_mode == 'XYZ' else None,
                    "stored_rot_quat": obj.rotation_quaternion.copy() if obj.rotation_mode == 'QUATERNION' else None,
                    "stored_rot_axis_angle": obj.rotation_axis_angle[:] if obj.rotation_mode == 'AXIS_ANGLE' else None,
                    "stored_scale": obj.scale.copy(),
                }
                
            # --- 3. DETACH HIERARCHY ---
            select_and_activate(targets)
            # This keeps them visually in place while detaching
            bpy.ops.object.parent_clear(type='CLEAR_KEEP_TRANSFORM')

            # --- 4. ISOLATE DATA ---
            if props.isolate_data:
                geo_objs = [o for o in targets if o.type in {'MESH', 'CURVE', 'FONT'} and o.data]
                if geo_objs:
                    select_and_activate(geo_objs)
                    bpy.ops.object.make_single_user(type='SELECTED_OBJECTS', object=True, obdata=True)

            # --- 5. APPLY TRANSFORMS ---
            wm.progress_update(30)
            
            # Geometry objects can have transforms baked into vertices
            group_bake = [o for o in targets if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META', 'ARMATURE'}]
            
            # Non-geometry objects (Cameras, Lights, Empties) CANNOT have location/rotation "Applied"
            # without physically moving them. We only process them if we want to force-reset Scale.
            group_manual = [o for o in targets if o.type in {'EMPTY', 'LIGHT', 'CAMERA', 'SPEAKER'}]

            # Animated object handling
            if props.animated_handling == 'SKIP_ALL':
                all_transform_targets = [o for o in (group_bake + group_manual) if not relationships[o]["has_animation"]]
                skipped_animated = len(group_bake + group_manual) - len(all_transform_targets)
                group_bake = [o for o in group_bake if not relationships[o]["has_animation"]]
                group_manual = [o for o in group_manual if not relationships[o]["has_animation"]]
            elif props.animated_handling == 'SCALE_ONLY':
                for obj in list(group_bake):
                    if relationships[obj]["has_animation"] and (props.apply_loc or props.apply_rot):
                        animation_warnings += 1
                # For animated objects, only apply scale; loc/rot are forced off in the apply call below.
            elif props.animated_handling == 'FULL':
                if props.apply_loc or props.apply_rot:
                    animation_warnings = sum(1 for o in targets if relationships[o]["has_animation"])

            # Batch apply for geometry
            if group_bake:
                select_and_activate(group_bake)
                apply_loc = props.apply_loc
                apply_rot = props.apply_rot
                apply_scale = props.apply_scale

                # In SCALE_ONLY mode we run two passes to avoid touching loc/rot on animated objects.
                if props.animated_handling == 'SCALE_ONLY':
                    animated_geo = [o for o in group_bake if relationships[o]["has_animation"]]
                    static_geo = [o for o in group_bake if not relationships[o]["has_animation"]]

                    if static_geo:
                        select_and_activate(static_geo)
                        bpy.ops.object.transform_apply(
                            location=apply_loc,
                            rotation=apply_rot,
                            scale=apply_scale
                        )
                    if animated_geo and apply_scale:
                        select_and_activate(animated_geo)
                        bpy.ops.object.transform_apply(
                            location=False,
                            rotation=False,
                            scale=True
                        )
                else:
                    bpy.ops.object.transform_apply(
                        location=apply_loc,
                        rotation=apply_rot,
                        scale=apply_scale
                    )
            # Manual reset for Empties/Cameras
            # We avoid location/rotation resets to prevent visual teleports.
            for obj in group_manual:
                if props.apply_scale:
                    obj.scale = (1.0, 1.0, 1.0)

            # --- 6. CORRECT ANIMATION ---
            wm.progress_update(50)
            if props.correct_scale_keys:
                for obj in targets:
                    if not relationships[obj]["has_animation"]:
                        continue
                    if props.animated_handling == 'SKIP_ALL':
                        continue
                    if not obj.animation_data or not obj.animation_data.action:
                        continue

                    action = obj.animation_data.action
                    data = relationships[obj]
                    s_scale = data["stored_scale"]

                    for fcurve in action.fcurves:
                        if props.apply_scale and fcurve.data_path.endswith("scale"):
                            idx = fcurve.array_index
                            if idx < 3 and abs(s_scale[idx]) > 1e-6:
                                for kf in fcurve.keyframe_points:
                                    kf.co[1] /= s_scale[idx]
                                    kf.handle_left[1] /= s_scale[idx]
                                    kf.handle_right[1] /= s_scale[idx]
                            fcurve.update()

            # --- 7. NORMALS FIX ---
            if props.fix_normals and props.apply_scale:
                wm.progress_update(60)
                mesh_objs = [o for o in targets if o.type == 'MESH']
                if mesh_objs:
                    ensure_object_mode()
                    select_and_activate(mesh_objs)
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.mesh.normals_make_consistent(inside=False)
                    bpy.ops.object.mode_set(mode='OBJECT')

            # --- 8. REBUILD HIERARCHY (Optimized) ---
            print("Step 8: Rebuilding Hierarchy...")
            wm.progress_update(70)

            ensure_object_mode()
            bpy.ops.object.select_all(action='DESELECT')

            count = 0
            total = len(relationships)

            for obj, data in relationships.items():
                parent = data["parent"]

                # We always enforce the stored World Matrix.
                # This ensures children stay visually stable.
                original_mw = data["stored_matrix_world"]

                if parent:
                    obj.parent = parent
                    obj.parent_type = data["parent_type"]
                    if data["parent_bone"] and data["parent_type"] == 'BONE':
                        obj.parent_bone = data["parent_bone"]

                obj.matrix_world = original_mw

                count += 1
                if count % 20 == 0:
                    wm.progress_update(70 + int((count / total) * 25))

            # --- 9. RESTORE VISIBILITY ---
            wm.progress_update(99)
            for obj, state in visibility_state.items():
                try:
                    if state['global']:
                        obj.hide_viewport = True
                    if state['local']:
                        obj.hide_set(True)
                except Exception as visibility_error:
                    print(f"Visibility restore warning for {obj.name}: {visibility_error}")

        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, f"Smart Apply failed: {exc}")
            return {'CANCELLED'}
        finally:
            wm.progress_end()
            if window:
                window.cursor_modal_restore()

        info_parts = [f"Smart Apply Finished: {len(targets)} objects."]
        if skipped_animated:
            info_parts.append(f"Skipped animated: {skipped_animated}.")
        if animation_warnings:
            info_parts.append(f"Animated risk warning on {animation_warnings} object(s).")
        self.report({'INFO'}, " ".join(info_parts))
        return {'FINISHED'}

class VIEW3D_PT_smart_apply_ui(bpy.types.Panel):
    bl_label = "Smart Apply Scene"
    bl_idname = "VIEW3D_PT_smart_apply_ui"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Clean Scene"

    def draw(self, context):
        layout = self.layout
        props = context.scene.smart_apply_props
        
        box = layout.box()
        box.label(text="Apply Transforms:", icon='SCENE_DATA')
        col = box.column(align=True)
        col.prop(props, "apply_loc")
        col.prop(props, "apply_rot")
        col.prop(props, "apply_scale")
        
        layout.separator()
        
        box2 = layout.box()
        box2.label(text="Safety:", icon='LOCKED')
        col2 = box2.column(align=True)
        col2.prop(props, "isolate_data", text="Isolate Multi-User")
        col2.prop(props, "fix_normals", text="Fix Normals")
        col2.prop(props, "correct_scale_keys", text="Correct Scale Keyframes")

        layout.separator()

        box3 = layout.box()
        box3.label(text="Animated Objects:", icon='ANIM')
        col3 = box3.column(align=True)
        col3.prop(props, "animated_handling", text="")

        row = layout.row()
        row.scale_y = 1.5
        row.operator("object.smart_apply_instant", text="Execute Smart Apply", icon='CHECKMARK')

class SmartApplySettings(bpy.types.PropertyGroup):
    apply_loc: bpy.props.BoolProperty(name="Location", default=False)
    apply_rot: bpy.props.BoolProperty(name="Rotation", default=True)
    apply_scale: bpy.props.BoolProperty(name="Scale", default=True)
    isolate_data: bpy.props.BoolProperty(name="Isolate Multi-User", default=True)
    fix_normals: bpy.props.BoolProperty(name="Fix Normals", default=True)
    correct_scale_keys: bpy.props.BoolProperty(
        name="Correct Scale Keyframes",
        description="When scale is applied, divide scale keyframe values by original scale",
        default=True
    )
    animated_handling: bpy.props.EnumProperty(
        name="Animated Handling",
        description="How to handle objects that have f-curves",
        items=[
            ('SKIP_ALL', "Skip Animated Objects", "Do not apply transforms to animated objects"),
            ('SCALE_ONLY', "Scale-Only on Animated", "Apply only scale on animated objects"),
            ('FULL', "Process All (Risky)", "Apply requested transforms to animated objects"),
        ],
        default='SCALE_ONLY'
    )

classes = (
    OBJECT_OT_smart_apply_instant,
    VIEW3D_PT_smart_apply_ui,
    SmartApplySettings,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.smart_apply_props = bpy.props.PointerProperty(type=SmartApplySettings)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.smart_apply_props

if __name__ == "__main__":
    register()